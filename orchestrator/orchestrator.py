"""Chaturbate live watcher -> IG/CB orchestrator.

- Polls Chaturbate Events API per model
- Stops IG bot when any model is live
- Starts CB viewer bot for a selected live model
- Restarts IG when all models are offline
"""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urljoin

import aiohttp
import requests

try:
    import socketio
except Exception:  # pragma: no cover
    socketio = None

CONFIG_ENV_VAR = "ORCH_CONFIG"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


@dataclass
class ModelConfig:
    label: str
    username: str
    token: str
    events_url: str


@dataclass
class ModelState:
    live: bool = False
    last_change_ts: float = 0.0
    last_event_ts: float = 0.0
    last_event_id: str = ""
    last_source: str = ""
    next_url: str = ""
    last_stats_ts: float = 0.0


class IGClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.last_login_ts = 0.0

    def _login(self) -> bool:
        if not self.username or not self.password:
            logging.error("IG login missing username or password")
            return False
        try:
            self.session.get(f"{self.base_url}/login", timeout=self.timeout)
            resp = self.session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
                allow_redirects=True,
            )
            ok = resp.status_code in (200, 302)
            self.last_login_ts = time.time()
            return ok
        except Exception as exc:
            logging.error("IG login failed: %s", exc)
            return False

    def ensure_login(self) -> bool:
        if time.time() - self.last_login_ts < 15 * 60:
            return True
        return self._login()

    def start(self) -> bool:
        if not self.ensure_login():
            return False
        try:
            resp = self.session.get(f"{self.base_url}/start", timeout=self.timeout)
            return resp.status_code in (200, 302)
        except Exception as exc:
            logging.error("IG start failed: %s", exc)
            return False

    def stop(self) -> bool:
        if not self.ensure_login():
            return False
        try:
            resp = self.session.get(f"{self.base_url}/stop?mode=dm", timeout=self.timeout)
            return resp.status_code in (200, 302)
        except Exception as exc:
            logging.error("IG stop failed: %s", exc)
            return False


class CBClient:
    def __init__(self, base_url: str, socket_url: str, username: str, password: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.socket_url = socket_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.token = ""
        self.token_ts = 0.0

    def _login(self) -> bool:
        if not self.username or not self.password:
            logging.error("CB login missing username or password")
            return False
        try:
            resp = self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logging.error("CB login failed: status=%s", resp.status_code)
                return False
            data = resp.json()
            token = data.get("token", "")
            if not token:
                logging.error("CB login failed: token missing")
                return False
            self.token = token
            self.token_ts = time.time()
            return True
        except Exception as exc:
            logging.error("CB login failed: %s", exc)
            return False

    def ensure_token(self) -> bool:
        if self.token and time.time() - self.token_ts < 12 * 60 * 60:
            return True
        return self._login()

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        if not self.ensure_token():
            return None
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code == 401:
                if not self._login():
                    return None
                headers["Authorization"] = f"Bearer {self.token}"
                resp = self.session.request(method, url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code >= 400:
                logging.error("CB request failed: %s %s status=%s", method, path, resp.status_code)
                return None
            if resp.text:
                return resp.json()
            return {}
        except Exception as exc:
            logging.error("CB request error: %s", exc)
            return None

    def set_target(self, username: str, description: str = "") -> bool:
        data = self._request("GET", "/api/targets")
        if data is None:
            return False

        targets = data if isinstance(data, list) else []
        normalized = username.strip().lower()
        found = None
        for t in targets:
            if str(t.get("username", "")).strip().lower() == normalized:
                found = t
                break

        updates: List[dict] = []
        for t in targets:
            enabled = str(t.get("username", "")).strip().lower() == normalized
            if bool(t.get("enabled", True)) != enabled:
                updates.append({
                    "id": t.get("id"),
                    "username": t.get("username"),
                    "description": t.get("description", ""),
                    "enabled": enabled,
                })

        if found is None:
            updates.append({
                "id": None,
                "username": username,
                "description": description,
                "enabled": True,
            })

        for item in updates:
            if not item.get("username"):
                continue
            res = self._request("POST", "/api/targets", item)
            if res is None:
                return False

        return True

    def _emit_socket(self, event_name: str, data: Optional[dict] = None) -> bool:
        if socketio is None:
            logging.error("python-socketio is not installed")
            return False
        if not self.ensure_token():
            return False

        client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        try:
            client.connect(self.socket_url, auth={"token": self.token}, wait_timeout=10)
            client.emit(event_name, data or {})
            time.sleep(1)
            client.disconnect()
            return True
        except Exception as exc:
            logging.error("CB socket emit failed: %s", exc)
            try:
                client.disconnect()
            except Exception:
                pass
            return False

    def start_viewers(self, target_username: str, description: str = "") -> bool:
        if not self.set_target(target_username, description=description):
            return False
        return self._emit_socket("start-anon", {"target_username": target_username})

    def stop_viewers(self) -> bool:
        return self._emit_socket("stop-anon", {})


class Orchestrator:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.models = self._load_models(config)
        self.model_states: Dict[str, ModelState] = {m.username: ModelState() for m in self.models}
        self.state_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()

        ig_cfg = config.get("ig", {})
        cb_cfg = config.get("cb", {})
        timeout = int(config.get("polling", {}).get("http_timeout_sec", 20))

        self.ig = IGClient(
            base_url=os.environ.get("IG_BASE_URL", ig_cfg.get("base_url", "")),
            username=os.environ.get("IG_USERNAME", ig_cfg.get("username", "")),
            password=os.environ.get("IG_PASSWORD", ig_cfg.get("password", "")),
            timeout=timeout,
        )
        self.cb = CBClient(
            base_url=os.environ.get("CB_BASE_URL", cb_cfg.get("base_url", "")),
            socket_url=os.environ.get("CB_SOCKET_URL", cb_cfg.get("socket_url", "")),
            username=os.environ.get("CB_USERNAME", cb_cfg.get("username", "")),
            password=os.environ.get("CB_PASSWORD", cb_cfg.get("password", "")),
            timeout=timeout,
        )

        policy = config.get("policy", {})
        self.priority_usernames = policy.get("priority_usernames", [])

    @staticmethod
    def _load_models(config: dict) -> List[ModelConfig]:
        rows = config.get("models", [])
        models: List[ModelConfig] = []
        for row in rows:
            models.append(
                ModelConfig(
                    label=str(row.get("label", "")).strip(),
                    username=str(row.get("username", "")).strip(),
                    token=str(row.get("token", "")).strip(),
                    events_url=str(row.get("events_url", "")).strip(),
                )
            )
        return [m for m in models if m.username and m.events_url]

    async def set_live_state(self, username: str, live: bool, source: str, event_id: str = "") -> None:
        async with self.state_lock:
            state = self.model_states.get(username)
            if state is None:
                return
            if state.live != live:
                state.live = live
                state.last_change_ts = time.time()
                state.last_source = source
                label = "LIVE" if live else "OFFLINE"
                logging.info("%s -> %s (%s)", username, label, source)
            if event_id:
                state.last_event_id = event_id

    async def update_next_url(self, username: str, next_url: str) -> None:
        async with self.state_lock:
            state = self.model_states.get(username)
            if state is None:
                return
            state.next_url = next_url

    async def update_event_ts(self, username: str) -> None:
        async with self.state_lock:
            state = self.model_states.get(username)
            if state is None:
                return
            state.last_event_ts = time.time()

    async def update_stats_ts(self, username: str) -> None:
        async with self.state_lock:
            state = self.model_states.get(username)
            if state is None:
                return
            state.last_stats_ts = time.time()

    async def snapshot_states(self) -> Dict[str, ModelState]:
        async with self.state_lock:
            return {k: ModelState(**vars(v)) for k, v in self.model_states.items()}

    async def poll_events_for_model(self, model: ModelConfig, session: aiohttp.ClientSession) -> None:
        timeout_sec = int(self.config.get("polling", {}).get("events_timeout_sec", 12))
        stats_interval = int(self.config.get("polling", {}).get("stats_interval_sec", 60))
        backoff = 2

        next_url = model.events_url
        if not next_url.endswith("/"):
            next_url = f"{next_url}/"

        while not self.stop_event.is_set():
            try:
                async with session.get(next_url, timeout=aiohttp.ClientTimeout(total=timeout_sec + 5)) as resp:
                    if resp.status != 200:
                        logging.warning("Events API %s status=%s", model.username, resp.status)
                        await asyncio.sleep(backoff)
                        continue
                    payload = await resp.json()

                events = payload.get("events", [])
                for event in events:
                    method = str(event.get("method", "")).strip()
                    event_id = str(event.get("id", "")).strip()
                    if method == "broadcastStart":
                        await self.set_live_state(model.username, True, "events", event_id=event_id)
                    elif method == "broadcastStop":
                        await self.set_live_state(model.username, False, "events", event_id=event_id)

                await self.update_event_ts(model.username)

                next_url_raw = payload.get("nextUrl") or payload.get("next_url")
                if next_url_raw:
                    next_url = urljoin(model.events_url, next_url_raw)
                else:
                    next_url = model.events_url

                await self.update_next_url(model.username, next_url)

                if stats_interval > 0:
                    snapshot = await self.snapshot_states()
                    state = snapshot.get(model.username)
                    if state and time.time() - state.last_stats_ts >= stats_interval:
                        await self.check_stats(model, session)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.warning("Events poll error for %s: %s", model.username, exc)
                await asyncio.sleep(backoff)

    async def check_stats(self, model: ModelConfig, session: aiohttp.ClientSession) -> None:
        if not model.token:
            return
        url = f"https://chaturbate.com/statsapi/?username={model.username}&token={model.token}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
            live = parse_stats_live(data)
            if live is not None:
                await self.set_live_state(model.username, live, "stats")
            await self.update_stats_ts(model.username)
        except Exception:
            return

    def pick_target(self, live_usernames: List[str]) -> Optional[str]:
        if not live_usernames:
            return None
        if self.priority_usernames:
            for name in self.priority_usernames:
                if name in live_usernames:
                    return name
        return live_usernames[0]

    async def control_loop(self) -> None:
        interval = int(self.config.get("polling", {}).get("control_interval_sec", 5))
        current_mode: Optional[str] = None
        current_target = ""

        while not self.stop_event.is_set():
            snapshot = await self.snapshot_states()
            live_usernames = [u for u, s in snapshot.items() if s.live]
            target = self.pick_target(live_usernames)

            if target:
                if current_mode != "CB" or current_target != target:
                    logging.info("Switching to CB target: %s", target)
                    if current_mode != "CB":
                        await asyncio.to_thread(self.ig.stop)
                    await asyncio.to_thread(self.cb.start_viewers, target, "auto")
                    current_mode = "CB"
                    current_target = target
            else:
                if current_mode != "IG":
                    logging.info("Switching to IG mode")
                    await asyncio.to_thread(self.cb.stop_viewers)
                    await asyncio.to_thread(self.ig.start)
                    current_mode = "IG"
                    current_target = ""

            await asyncio.sleep(max(1, interval))

    async def run(self) -> None:
        logging.info("Orchestrator starting")
        async with aiohttp.ClientSession() as session:
            poll_tasks = [
                asyncio.create_task(self.poll_events_for_model(model, session))
                for model in self.models
            ]
            control_task = asyncio.create_task(self.control_loop())
            await self.stop_event.wait()
            for task in poll_tasks:
                task.cancel()
            control_task.cancel()


def parse_stats_live(data: dict) -> Optional[bool]:
    if not isinstance(data, dict):
        return None

    def _as_text(value) -> str:
        return str(value or "").strip().lower()

    for key in ("room_status", "status", "broadcaster_status", "cam_status"):
        if key in data:
            text = _as_text(data.get(key))
            if not text:
                continue
            if text in ("offline", "off", "stopped"):
                return False
            if text in ("public", "private", "away", "hidden", "group", "live"):
                return True

    for key in ("is_broadcasting", "broadcasting", "live"):
        if key in data:
            value = data.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            text = _as_text(value)
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False

    return None


def load_config() -> dict:
    path = os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    setup_logging()
    config = load_config()
    orchestrator = Orchestrator(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_stop(_sig, _frame):
        logging.info("Stop requested")
        orchestrator.stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    for sig_name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_stop)
        except (ValueError, OSError, RuntimeError):
            pass

    try:
        loop.run_until_complete(orchestrator.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
