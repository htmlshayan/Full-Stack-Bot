import os
import sys
import json
import time
import uuid
import socket
import platform
import asyncio
import logging
import subprocess
import psutil
from typing import Dict, Optional

import redis.asyncio as redis
from dotenv import load_dotenv
from core.chrome import lock_chrome_version, parse_version_major, resolve_chromedriver_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cb_bot.worker")

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
if os.path.isfile(ENV_PATH):
    load_dotenv(ENV_PATH)
    logger.info(f"Loaded environment from: {ENV_PATH}")
else:
    load_dotenv()
    logger.info("Loaded environment from system variables (no .env found).")

REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_QUEUE_KEY = os.getenv("REDIS_QUEUE_KEY", "cb:queue")
REDIS_EVENTS_CHANNEL = os.getenv("REDIS_EVENTS_CHANNEL", "cb:events")
REDIS_CONTROL_CHANNEL = os.getenv("REDIS_CONTROL_CHANNEL", "cb:control")
REDIS_WORKER_PREFIX = os.getenv("REDIS_WORKER_PREFIX", "cb:worker:")

WORKER_ID_RAW = os.getenv("WORKER_ID", "").strip()
WORKER_LABEL_RAW = os.getenv("WORKER_LABEL", "").strip()
WORKER_MAX_PROFILES = min(int(os.getenv("WORKER_MAX_PROFILES", "10")), 10)
WORKER_TILE_COLS = int(os.getenv("WORKER_TILE_COLS", "5"))
WORKER_HEARTBEAT_SECONDS = int(os.getenv("WORKER_HEARTBEAT_SECONDS", "5"))
WORKER_HEARTBEAT_TTL = int(os.getenv("WORKER_HEARTBEAT_TTL", "20"))

READY_LOG_PHRASE = "READY: target_loaded"

processes: Dict[int, Dict[str, object]] = {}
slot_lock = asyncio.Lock()
available_slots = list(range(max(1, WORKER_MAX_PROFILES)))
RESOLVED_CHROMEDRIVER_PATH = ""


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def update_env_file(path: str, key: str, value: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return False

    prefix = f"{key}="
    updated = False
    for idx, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[idx] = f"{key}={value}\n"
            updated = True
            break

    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")
        updated = True

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return True
    except Exception:
        return False


def resolve_worker_id(raw: str, env_path: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned and cleaned.lower() not in ("auto", "default"):
        return cleaned

    host = socket.gethostname().strip() or "worker"
    node_hex = f"{uuid.getnode():012x}"
    auto_id = f"{host}-{node_hex[-6:]}"
    os.environ["WORKER_ID"] = auto_id
    update_env_file(env_path, "WORKER_ID", auto_id)
    return auto_id


def resolve_worker_label(raw: str, worker_id: str, env_path: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned and cleaned.lower() not in ("auto", "default"):
        return cleaned
    os.environ["WORKER_LABEL"] = worker_id
    update_env_file(env_path, "WORKER_LABEL", worker_id)
    return worker_id


WORKER_ID = resolve_worker_id(WORKER_ID_RAW, ENV_PATH)
WORKER_LABEL = resolve_worker_label(WORKER_LABEL_RAW, WORKER_ID, ENV_PATH)

def normalize_platform(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("win"):
        return "windows"
    if text in ("darwin", "mac", "macos", "osx", "os x"):
        return "macos"
    if text.startswith("linux"):
        return "linux"
    return "unknown"

WORKER_PLATFORM = normalize_platform(platform.system())

def now_ts() -> int:
    return int(time.time())


def kill_process_tree(pid: int, timeout: float = 2.0) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    children = parent.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            continue
    try:
        parent.terminate()
    except psutil.Error:
        pass
    try:
        psutil.wait_procs([parent] + children, timeout=timeout)
    except psutil.Error:
        pass
    for proc in children:
        if proc.is_running():
            try:
                proc.kill()
            except psutil.Error:
                pass
    if parent.is_running():
        try:
            parent.kill()
        except psutil.Error:
            pass


def stop_single_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            kill_process_tree(proc.pid)
    except Exception as exc:
        logger.warning(f"Failed to stop process {proc.pid}: {exc}")


async def publish_event(redis_conn: redis.Redis, payload: dict) -> None:
    try:
        await redis_conn.publish(REDIS_EVENTS_CHANNEL, json.dumps(payload))
    except Exception as exc:
        logger.warning(f"Redis publish failed: {exc}")


async def claim_account(redis_conn: redis.Redis, run_id: str, account_id: str) -> bool:
    key = f"cb:run:{run_id}:accounts"
    try:
        added = await redis_conn.sadd(key, account_id)
        if added:
            await redis_conn.expire(key, 6 * 3600)
            return True
        return False
    except Exception as exc:
        logger.warning(f"Account claim failed for {account_id}: {exc}")
        return True


async def acquire_slot() -> Optional[int]:
    async with slot_lock:
        if not available_slots:
            return None
        return available_slots.pop(0)


async def release_slot(slot_index: int) -> None:
    async with slot_lock:
        if slot_index not in available_slots:
            available_slots.append(slot_index)


async def publish_browser_count(redis_conn: redis.Redis) -> None:
    await publish_event(redis_conn, {
        "type": "browser-count",
        "worker_id": WORKER_ID,
        "platform": WORKER_PLATFORM,
        "count": len(processes),
        "ts": now_ts(),
    })


async def stream_logs(
    redis_conn: redis.Redis,
    proc: subprocess.Popen,
    display_name: str,
    run_id: str,
    slot_index: int,
) -> None:
    if proc.stdout is None:
        return

    while True:
        line = await asyncio.to_thread(proc.stdout.readline)
        if not line:
            break
        if isinstance(line, bytes):
            line = line.decode(errors="replace")
        message = line.strip()

        if message:
            await publish_event(redis_conn, {
                "type": "log",
                "name": display_name,
                "level": "info",
                "data": message,
                "worker_id": WORKER_ID,
                "run_id": run_id,
            })

        if "MESSAGE_SENT:" in message:
            await publish_event(redis_conn, {
                "type": "message-sent",
                "worker_id": WORKER_ID,
                "run_id": run_id,
                "delta": 1,
            })

        if READY_LOG_PHRASE in message:
            await publish_event(redis_conn, {
                "type": "ready",
                "worker_id": WORKER_ID,
                "run_id": run_id,
                "name": display_name,
            })

    rc = proc.poll()
    meta = processes.pop(proc.pid, None)
    if meta:
        await release_slot(slot_index)

    await publish_event(redis_conn, {
        "type": "log",
        "name": "system",
        "level": "warning" if rc else "info",
        "data": f"Process {display_name} exited with code {rc}.",
        "worker_id": WORKER_ID,
        "run_id": run_id,
    })

    await publish_browser_count(redis_conn)


async def launch_task(redis_conn: redis.Redis, task: dict) -> bool:
    slot_index = await acquire_slot()
    if slot_index is None:
        return False

    account_id = str(task.get("account_id") or "unknown")
    account_username = str(task.get("account_username") or account_id)
    target_username = str(task.get("target_username") or "").strip()
    run_id = str(task.get("run_id") or "")

    if run_id:
        claimed = await claim_account(redis_conn, run_id, account_id)
        if not claimed:
            await publish_event(redis_conn, {
                "type": "log",
                "name": "system",
                "level": "warning",
                "data": f"Skipping {account_username} - already assigned to another worker.",
                "worker_id": WORKER_ID,
                "run_id": run_id,
            })
            await release_slot(slot_index)
            return False

    if not target_username:
        await publish_event(redis_conn, {
            "type": "log",
            "name": "system",
            "level": "error",
            "data": f"Task missing target for account {account_username}.",
            "worker_id": WORKER_ID,
            "run_id": run_id,
        })
        await release_slot(slot_index)
        return False

    cookies_obj = task.get("cookies")
    if isinstance(cookies_obj, str):
        try:
            cookies_obj = json.loads(cookies_obj)
        except json.JSONDecodeError:
            cookies_obj = None

    if isinstance(cookies_obj, dict) and "cookies" in cookies_obj:
        cookies_obj = cookies_obj.get("cookies")

    if not isinstance(cookies_obj, list):
        await publish_event(redis_conn, {
            "type": "log",
            "name": "system",
            "level": "error",
            "data": f"Invalid cookies for account {account_username}.",
            "worker_id": WORKER_ID,
            "run_id": run_id,
        })
        await release_slot(slot_index)
        return False

    temp_dir = os.path.join(ROOT_DIR, "data", "temp")
    os.makedirs(temp_dir, exist_ok=True)
    cookie_path = os.path.join(temp_dir, f"cookies_{account_id}_{uuid.uuid4().hex}.json")
    with open(cookie_path, "w", encoding="utf-8") as fp:
        json.dump(cookies_obj, fp)

    proxy = str(task.get("proxy") or "").strip()
    headless = parse_bool(task.get("headless"), False)

    msg_enabled = parse_bool(task.get("msg_enabled"), False)
    msg_min_seconds = int(task.get("msg_min_seconds") or 120)
    msg_max_seconds = int(task.get("msg_max_seconds") or 300)
    msg_texts = task.get("msg_texts") or []
    if not isinstance(msg_texts, list):
        msg_texts = []
    msg_enabled = bool(msg_enabled and msg_texts)

    cmd = [
        os.sys.executable,
        "bots/cookieviewer.py",
        target_username,
        cookie_path,
    ]
    if headless:
        cmd.append("--headless")
    if proxy:
        cmd.extend(["--proxy", proxy])

    display_name = f"{WORKER_ID}:{account_username}"

    await publish_event(redis_conn, {
        "type": "log",
        "name": "system",
        "level": "info",
        "data": f"Launching {display_name} (slot {slot_index}).",
        "worker_id": WORKER_ID,
        "run_id": run_id,
    })

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "CB_MSG_ENABLED": "1" if msg_enabled else "0",
        "CB_MSG_MIN_SECONDS": str(max(1, msg_min_seconds)),
        "CB_MSG_MAX_SECONDS": str(max(1, msg_max_seconds)),
        "CB_MSGS_JSON": json.dumps(msg_texts),
        "CB_TILE_INDEX": str(slot_index),
        "CB_TILE_TOTAL": str(max(1, WORKER_MAX_PROFILES)),
        "CB_TILE_COLS": str(max(1, WORKER_TILE_COLS)),
    }
    env["CHROMEDRIVER_PATH"] = RESOLVED_CHROMEDRIVER_PATH

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=ROOT_DIR,
        text=True,
        bufsize=1,
        env=env,
    )

    processes[proc.pid] = {
        "proc": proc,
        "slot": slot_index,
        "run_id": run_id,
        "account_id": account_id,
        "account_username": account_username,
        "cookie_path": cookie_path,
    }

    asyncio.create_task(stream_logs(redis_conn, proc, display_name, run_id, slot_index))
    await publish_browser_count(redis_conn)
    return True


async def stop_all(redis_conn: redis.Redis, run_id: Optional[str] = None) -> None:
    targets = []
    for pid, meta in list(processes.items()):
        if run_id and meta.get("run_id") != run_id:
            continue
        targets.append(meta)

    if not targets:
        return

    for meta in targets:
        proc = meta.get("proc")
        if isinstance(proc, subprocess.Popen):
            stop_single_process(proc)

    await publish_event(redis_conn, {
        "type": "log",
        "name": "system",
        "level": "warning",
        "data": f"Stop command received. Closed {len(targets)} process(es).",
        "worker_id": WORKER_ID,
        "run_id": run_id or "",
    })


async def control_listener(redis_conn: redis.Redis) -> None:
    pubsub = redis_conn.pubsub()
    await pubsub.subscribe(REDIS_CONTROL_CHANNEL)

    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        raw = message.get("data")
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        command = str(payload.get("command") or "").lower()
        run_id = payload.get("run_id")
        if command == "stop":
            await stop_all(redis_conn, run_id=run_id)
        elif command == "shutdown":
            await stop_all(redis_conn, run_id=run_id)
            break


async def heartbeat_loop(redis_conn: redis.Redis) -> None:
    while True:
        meta = {
            "id": WORKER_ID,
            "label": WORKER_LABEL,
            "max_profiles": WORKER_MAX_PROFILES,
            "running": len(processes),
            "ts": now_ts(),
        }
        try:
            await redis_conn.set(
                f"{REDIS_WORKER_PREFIX}{WORKER_ID}",
                json.dumps(meta),
                ex=max(1, WORKER_HEARTBEAT_TTL),
            )
        except Exception as exc:
            logger.warning(f"Worker heartbeat failed: {exc}")
        await publish_browser_count(redis_conn)
        await asyncio.sleep(max(1, WORKER_HEARTBEAT_SECONDS))


async def queue_worker(redis_conn: redis.Redis) -> None:
    queue_keys = [f"{REDIS_QUEUE_KEY}:{WORKER_ID}", REDIS_QUEUE_KEY]
    while True:
        if len(processes) >= WORKER_MAX_PROFILES:
            await asyncio.sleep(1)
            continue

        item = await redis_conn.blpop(queue_keys, timeout=2)
        if not item:
            continue
        _key, payload = item
        try:
            task = json.loads(payload)
        except Exception:
            continue

        launched = await launch_task(redis_conn, task)
        if not launched:
            await asyncio.sleep(1)


async def main() -> None:
    global RESOLVED_CHROMEDRIVER_PATH
    if not REDIS_URL:
        logger.error("REDIS_URL is not set. Worker cannot start.")
        sys.exit(1)

    logger.info("Locking Chrome major version to installed Chrome...")
    locked_version = lock_chrome_version(ENV_PATH)
    if locked_version:
        logger.info(f"CHROME_VERSION_MAIN resolved: {locked_version}")
    else:
        logger.info("Chrome major version not detected; leaving CHROME_VERSION_MAIN unset.")

    logger.info("Resolving ChromeDriver path...")
    required_major = parse_version_major(os.getenv("CHROME_VERSION_MAIN", ""))
    driver_path = resolve_chromedriver_path(required_major)
    if driver_path:
        os.environ["CHROMEDRIVER_PATH"] = driver_path
        RESOLVED_CHROMEDRIVER_PATH = driver_path
        logger.info(f"Auto-resolved CHROMEDRIVER_PATH: {driver_path}")
    else:
        os.environ.pop("CHROMEDRIVER_PATH", None)
        RESOLVED_CHROMEDRIVER_PATH = ""
        logger.info("No matching ChromeDriver found; using undetected_chromedriver auto.")

    logger.info("Checking Redis connectivity...")
    redis_conn = redis.from_url(REDIS_URL, decode_responses=True)
    await redis_conn.ping()
    logger.info("REDIS_PING_OK")

    logger.info(f"Connected to Redis at {REDIS_URL}")
    logger.info(f"Worker {WORKER_ID} online. Max profiles: {WORKER_MAX_PROFILES}")

    tasks = [
        asyncio.create_task(heartbeat_loop(redis_conn)),
        asyncio.create_task(control_listener(redis_conn)),
        asyncio.create_task(queue_worker(redis_conn)),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(main())
