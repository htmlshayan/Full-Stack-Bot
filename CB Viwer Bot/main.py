import os
import shutil
import json
import asyncio
import subprocess
import hashlib
import secrets
import time
import platform
from datetime import datetime, timedelta
from typing import Dict, Optional
from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.future import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import init_db, get_db, AccountModel, SettingsModel, EmployeeModel, SessionLocal, TargetModel
from core.chrome import lock_chrome_version, parse_version_major, resolve_chromedriver_path
from contextlib import asynccontextmanager
import socketio
import uuid
import httpx
import psutil
import redis.asyncio as redis
from dotenv import load_dotenv

# ── Auth ──────────────────────────────────────────────────────────────────────
DEFAULT_USERNAME     = "bey"
DEFAULT_PASSWORD     = "#beycbbot!"
SESSION_DURATION     = timedelta(hours=24)
sessions: Dict[str, Dict[str, object]] = {}   # token -> {expiry, role}
socket_tokens: Dict[str, Dict[str, str]] = {}   # sid -> {token, role}

ENV_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), ".env")
if os.path.isfile(ENV_PATH):
    load_dotenv(ENV_PATH)
    print(f"[INFO] Loaded environment from: {ENV_PATH}")
else:
    load_dotenv()
    print("[INFO] Loaded environment from system variables (no .env found).")

# ── Distributed (Redis) Settings ─────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_ENABLED = bool(REDIS_URL)
REDIS_QUEUE_KEY = os.getenv("REDIS_QUEUE_KEY", "cb:queue")
REDIS_EVENTS_CHANNEL = os.getenv("REDIS_EVENTS_CHANNEL", "cb:events")
REDIS_CONTROL_CHANNEL = os.getenv("REDIS_CONTROL_CHANNEL", "cb:control")
REDIS_WORKER_PREFIX = os.getenv("REDIS_WORKER_PREFIX", "cb:worker:")

worker_browser_counts: Dict[str, int] = {}
worker_browser_platforms: Dict[str, str] = {}
distributed_run_active: bool = False
distributed_run_id: Optional[str] = None
MAX_PROFILES_PER_SERVER = 10

def normalize_platform(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("win"):
        return "windows"
    if text in ("darwin", "mac", "macos", "osx", "os x"):
        return "macos"
    if text.startswith("linux"):
        return "linux"
    return "unknown"

LOCAL_PLATFORM = normalize_platform(platform.system())

def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def normalize_token(raw: str) -> str:
    return raw.removeprefix("Bearer ").strip()

def request_token(request: Request) -> str:
    header_token = normalize_token(request.headers.get("Authorization", ""))
    if header_token:
        return header_token
    return (request.cookies.get("auth_token") or "").strip()

def is_https_request(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"

def is_token_valid(token: str) -> bool:
    now = datetime.now()
    entry = sessions.get(token)
    if not token or not entry or entry.get("expiry") < now:
        sessions.pop(token, None)
        return False
    entry["expiry"] = now + SESSION_DURATION   # rolling refresh
    return True

def session_role(token: str) -> str:
    entry = sessions.get(token) or {}
    return str(entry.get("role", ""))

def is_admin_token(token: str) -> bool:
    return session_role(token) == "admin"

def socket_auth_token(auth) -> str:
    if not isinstance(auth, dict):
        return ""
    token = auth.get("token", "")
    return str(token).strip()

def cookie_token_from_environ(environ) -> str:
    raw = environ.get("HTTP_COOKIE", "")
    if not raw:
        return ""
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == "auth_token":
            return value.strip()
    return ""

async def init_auth(db: AsyncSession):
    """Seed default credentials if they don't exist yet."""
    if not await db.get(SettingsModel, "auth_username"):
        db.add(SettingsModel(key="auth_username", value=DEFAULT_USERNAME))
    if not await db.get(SettingsModel, "auth_password_hash"):
        db.add(SettingsModel(key="auth_password_hash", value=hash_pw(DEFAULT_PASSWORD)))

    employee_user_row = await db.get(SettingsModel, "employee_username")
    employee_hash_row = await db.get(SettingsModel, "employee_password_hash")
    if employee_user_row and employee_hash_row:
        result = await db.execute(select(EmployeeModel).where(EmployeeModel.username == employee_user_row.value))
        if not result.scalar_one_or_none():
            db.add(EmployeeModel(
                id=str(uuid.uuid4()),
                username=employee_user_row.value,
                password_hash=employee_hash_row.value,
                role="employee"
            ))
    await db.commit()

# Account model for API
class Account(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: Optional[str] = None
    username: str
    password: str
    proxies: Optional[str] = ""
    cookies: Optional[str] = ""
    enabled: Optional[bool] = True

class Target(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[str] = None
    username: str
    description: Optional[str] = ""
    enabled: Optional[bool] = True

def total_browser_count() -> int:
    distributed = sum(worker_browser_counts.values()) if worker_browser_counts else 0
    local = sum(1 for p in processes.get("cookie", []) if p.poll() is None)
    return distributed + local

def browser_counts_by_platform() -> Dict[str, int]:
    counts: Dict[str, int] = {
        "windows": 0,
        "macos": 0,
        "linux": 0,
        "unknown": 0,
    }
    local_count = sum(1 for p in processes.get("cookie", []) if p.poll() is None)
    counts[LOCAL_PLATFORM] = counts.get(LOCAL_PLATFORM, 0) + local_count
    for worker_id, count in worker_browser_counts.items():
        platform_name = worker_browser_platforms.get(worker_id, "unknown")
        counts[platform_name] = counts.get(platform_name, 0) + count
    return counts

def build_browser_count_payload(include_system: bool = False) -> Dict[str, object]:
    count = total_browser_count()
    platform_counts = browser_counts_by_platform()
    payload: Dict[str, object] = {
        "count": count,
        "windows": platform_counts.get("windows", 0),
        "macos": platform_counts.get("macos", 0),
        "platform": LOCAL_PLATFORM,
    }
    if include_system:
        payload["cpu"] = psutil.cpu_percent()
        payload["ram"] = psutil.virtual_memory().percent
    return payload

async def track_browsers():
    """Count only the Selenium browser processes we spawned and track system resources."""
    while True:
        try:
            await sio.emit('browser-count', build_browser_count_payload(include_system=True))
        except Exception as e:
            print(f"Browser tracking error: {e}")
        await asyncio.sleep(5)

async def fetch_active_workers(redis_conn: redis.Redis) -> list:
    workers = []
    pattern = f"{REDIS_WORKER_PREFIX}*"
    async for key in redis_conn.scan_iter(match=pattern):
        raw = await redis_conn.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        worker_id = data.get("id") or key.replace(REDIS_WORKER_PREFIX, "")
        data["id"] = worker_id
        workers.append(data)
    return workers

async def publish_control(redis_conn: redis.Redis, command: str, run_id: str = "") -> None:
    payload = {"command": command, "run_id": run_id}
    await redis_conn.publish(REDIS_CONTROL_CHANNEL, json.dumps(payload))

async def redis_event_listener(redis_conn: redis.Redis) -> None:
    pubsub = redis_conn.pubsub()
    await pubsub.subscribe(REDIS_EVENTS_CHANNEL)
    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        raw = message.get("data")
        try:
            event = json.loads(raw)
        except Exception:
            continue

        evt_type = str(event.get("type") or "").lower()
        if evt_type == "log":
            await sio.emit('log', {
                'name': event.get("name", "worker"),
                'type': str(event.get("level") or "info"),
                'data': event.get("data", "")
            })
        elif evt_type == "browser-count":
            worker_id = str(event.get("worker_id") or "")
            if worker_id:
                try:
                    worker_browser_counts[worker_id] = int(event.get("count") or 0)
                except (TypeError, ValueError):
                    worker_browser_counts[worker_id] = 0
                raw_platform = event.get("platform")
                if raw_platform:
                    worker_browser_platforms[worker_id] = normalize_platform(raw_platform)
        elif evt_type == "message-sent":
            try:
                delta = int(event.get("delta") or 1)
            except (TypeError, ValueError):
                delta = 1
            stats = await bump_message_stats(delta)
            await sio.emit('message-stats', stats)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as db:
        await init_auth(db)
    print("[INFO] Locking Chrome major version to installed Chrome...")
    locked_version = lock_chrome_version(ENV_PATH)
    if locked_version:
        print(f"[INFO] CHROME_VERSION_MAIN resolved: {locked_version}")
    else:
        print("[INFO] Chrome major version not detected; leaving CHROME_VERSION_MAIN unset.")
    app.state.redis = None
    app.state.bg_tasks = []
    if REDIS_ENABLED:
        try:
            print("[INFO] Checking Redis connectivity...")
            app.state.redis = redis.from_url(REDIS_URL, decode_responses=True)
            await app.state.redis.ping()
            print("REDIS_PING_OK")
            print(f"[INFO] Connected to Redis at {REDIS_URL}")
            print("[INFO] Syncing Redis settings in database...")
            try:
                async with SessionLocal() as db:
                    await sync_redis_settings(db)
                print("REDIS_SETTINGS_SYNCED")
            except Exception as exc:
                print(f"[ERROR] REDIS_SETTINGS_SYNC_FAILED: {exc}")
            print("[INFO] Resolving ChromeDriver path...")
            required_major = parse_version_major(os.getenv("CHROME_VERSION_MAIN", ""))
            driver_path = resolve_chromedriver_path(required_major)
            if driver_path:
                os.environ["CHROMEDRIVER_PATH"] = driver_path
                print(f"[INFO] Auto-resolved CHROMEDRIVER_PATH: {driver_path}")
            else:
                print("[INFO] No matching ChromeDriver found; using undetected_chromedriver auto.")
            app.state.bg_tasks.append(asyncio.create_task(redis_event_listener(app.state.redis)))
        except Exception as exc:
            print(f"[ERROR] Redis connection failed: {exc}")
            app.state.redis = None
    app.state.bg_tasks.append(asyncio.create_task(track_browsers()))
    yield
    for task in app.state.bg_tasks:
        task.cancel()
    if app.state.redis:
        await app.state.redis.close()

app = FastAPI(lifespan=lifespan)

# ── Auth Middleware ───────────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Always allow: login endpoint, login page, static assets, socket.io
    public = ("/api/login", "/login.html", "/socket.io", "/style.css", "/app.js")
    if any(path.startswith(p) for p in public):
        return await call_next(request)

    # Protect all /api/* routes
    if path.startswith("/api/"):
        token = request_token(request)
        if not is_token_valid(token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        admin_only = ("/api/settings", "/api/change-password", "/api/employees")
        if any(path.startswith(p) for p in admin_only) and not is_admin_token(token):
            return JSONResponse({"error": "Forbidden"}, status_code=403)

    return await call_next(request)

@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".js") or path.endswith(".css") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, app)


# Bot processes management
# We store lists of processes to support multiple instances
processes: Dict[str, list] = {
    "anon": [],
    "cookie": []
}

READY_LOG_PHRASE = "READY: target_loaded"
READY_WAIT_TIMEOUT = 90
MAX_LAUNCH_ATTEMPTS = 1
launch_cancel_event: Optional[asyncio.Event] = None
launch_in_progress: bool = False
COOKIE_VIEWER_HEADLESS = os.getenv("COOKIE_VIEWER_HEADLESS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

MESSAGE_STAT_TOTAL_KEY = "msg_total_count"
MESSAGE_STAT_TODAY_KEY = "msg_today_count"
MESSAGE_STAT_DATE_KEY = "msg_today_date"
MESSAGE_ENABLED_KEY = "msg_enabled"
MESSAGE_MIN_MINUTES_KEY = "msg_min_minutes"
MESSAGE_MAX_MINUTES_KEY = "msg_max_minutes"
MESSAGE_TEXTS_KEY = "msg_texts"

def parse_bool_setting(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default

def parse_int_setting(value: Optional[str], default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed

def normalize_enabled_value(value: Optional[object]) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("0", "false", "no", "n", "off"):
        return False
    if text in ("1", "true", "yes", "y", "on"):
        return True
    return True

def distribute_tasks_evenly(tasks: list, worker_caps: Dict[str, int]) -> Dict[str, list]:
    worker_ids = [wid for wid, cap in worker_caps.items() if cap > 0]
    if not tasks or not worker_ids:
        return {}

    worker_ids.sort()
    caps = {wid: worker_caps[wid] for wid in worker_ids}
    total_capacity = sum(caps.values())
    tasks = tasks[:total_capacity]

    base = len(tasks) // len(worker_ids)
    remainder = len(tasks) % len(worker_ids)
    target_counts: Dict[str, int] = {}
    for idx, wid in enumerate(worker_ids):
        target = base + (1 if idx < remainder else 0)
        target_counts[wid] = min(target, caps[wid])

    remaining = len(tasks) - sum(target_counts.values())
    while remaining > 0:
        progressed = False
        for wid in worker_ids:
            if remaining <= 0:
                break
            if target_counts[wid] < caps[wid]:
                target_counts[wid] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break

    queue_map: Dict[str, list] = {wid: [] for wid in worker_ids}
    index = 0
    for wid in worker_ids:
        count = target_counts.get(wid, 0)
        if count <= 0:
            continue
        queue_map[wid] = tasks[index:index + count]
        index += count

    return queue_map

def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")

async def upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    setting = await db.get(SettingsModel, key)
    if setting:
        setting.value = value
    else:
        db.add(SettingsModel(key=key, value=value))

async def sync_redis_settings(db: AsyncSession) -> None:
    await upsert_setting(db, "redis_url", REDIS_URL)
    await upsert_setting(db, "redis_queue_key", REDIS_QUEUE_KEY)
    await upsert_setting(db, "redis_events_channel", REDIS_EVENTS_CHANNEL)
    await upsert_setting(db, "redis_control_channel", REDIS_CONTROL_CHANNEL)
    await upsert_setting(db, "redis_worker_prefix", REDIS_WORKER_PREFIX)
    await db.commit()

async def load_message_stats(db: AsyncSession, reset_day: bool = True) -> Dict[str, int]:
    total_row = await db.get(SettingsModel, MESSAGE_STAT_TOTAL_KEY)
    today_row = await db.get(SettingsModel, MESSAGE_STAT_TODAY_KEY)
    date_row = await db.get(SettingsModel, MESSAGE_STAT_DATE_KEY)

    total = parse_int_setting(total_row.value if total_row else None, 0, min_value=0)
    today = parse_int_setting(today_row.value if today_row else None, 0, min_value=0)
    current_day = today_key()
    stored_day = (date_row.value or "") if date_row else ""

    if reset_day and stored_day != current_day:
        today = 0
        await upsert_setting(db, MESSAGE_STAT_TODAY_KEY, "0")
        await upsert_setting(db, MESSAGE_STAT_DATE_KEY, current_day)
        await db.commit()

    return {"total": total, "today": today}

async def bump_message_stats(delta: int = 1) -> Dict[str, int]:
    async with SessionLocal() as db:
        stats = await load_message_stats(db, reset_day=True)
        total = stats["total"] + delta
        today = stats["today"] + delta
        await upsert_setting(db, MESSAGE_STAT_TOTAL_KEY, str(total))
        await upsert_setting(db, MESSAGE_STAT_TODAY_KEY, str(today))
        await upsert_setting(db, MESSAGE_STAT_DATE_KEY, today_key())
        await db.commit()
        return {"total": total, "today": today}

def count_running(name: str) -> int:
    return sum(1 for proc in processes.get(name, []) if proc.poll() is None)

def is_anon_running() -> bool:
    return distributed_run_active or total_browser_count() > 0 or count_running("anon") > 0

def cleanup_old_profiles() -> None:
    profile_root = os.path.join("data", "temp", "profiles")
    if not os.path.isdir(profile_root):
        return
    try:
        shutil.rmtree(profile_root)
        print(f"Removed old profiles: {profile_root}")
    except Exception as exc:
        print(f"Failed to remove old profiles at {profile_root}: {exc}")

async def stream_logs(
    name: str,
    process: subprocess.Popen,
    display_name: Optional[str] = None,
    ready_event: Optional[asyncio.Event] = None,
    ready_phrase: Optional[str] = None,
    acc_id: Optional[str] = None,
    slot_index: Optional[int] = None
):
    """Stream stdout/stderr from process to Socket.io."""
    log_name = display_name or name
    if process.stdout is None:
        return
    while True:
        line = await asyncio.to_thread(process.stdout.readline)
        if not line:
            break
        if isinstance(line, bytes):
            line = line.decode(errors='replace')
        message = line.strip()
        if ready_event and ready_phrase and ready_phrase in message:
            ready_event.set()
        if "MESSAGE_SENT:" in message:
            stats = await bump_message_stats(1)
            await sio.emit('message-stats', stats)
        await sio.emit('log', {'name': log_name, 'type': 'info', 'data': message})
    
    # Process ended
    rc = process.poll()
    if process in processes.get(name, []):
        processes[name].remove(process)

    if rc != 0 and name == "cookie" and acc_id and not (launch_cancel_event and launch_cancel_event.is_set()):
        await sio.emit('log', {'name': 'system', 'type': 'warning',
            'data': f'Process for {display_name} exited with code {rc}. Automatic restart is currently disabled to prevent boot loops, please restart from the panel if needed.'})
    
    # Only emit stopped if no more processes of this name are running
    if not processes.get(name):
        await sio.emit('status', {'name': name, 'status': 'stopped', 'code': rc})
        if name == "cookie" and not is_anon_running():
            await sio.emit('status', {'name': 'anon', 'status': 'stopped', 'code': rc})

async def wait_for_process_ready(
    process: subprocess.Popen,
    ready_event: asyncio.Event,
    timeout: int,
    cancel_event: Optional[asyncio.Event] = None,
) -> bool:
    """Wait for a READY log signal or process exit."""
    start = time.monotonic()
    while True:
        if cancel_event and cancel_event.is_set():
            return False
        if ready_event.is_set():
            return True
        if process.poll() is not None:
            return False
        if time.monotonic() - start >= timeout:
            return False
        await asyncio.sleep(0.5)

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


def stop_process(name: str):
    if processes.get(name):
        print(f"Stopping {len(processes[name])} {name} processes...")
        for proc in processes[name]:
            stop_single_process(proc)
        processes[name] = []


def stop_single_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, check=False)
        else:
            kill_process_tree(proc.pid)
    except Exception as e:
        print(f"Error stopping process: {e}")

async def send_telegram(message: str, db: AsyncSession):
    """Send a message to all configured Telegram chat IDs."""
    try:
        # Get settings
        token_res = await db.execute(select(SettingsModel).where(SettingsModel.key == "tg_token"))
        chat_res = await db.execute(select(SettingsModel).where(SettingsModel.key == "tg_chats"))
        
        token = token_res.scalar_one_or_none()
        chats = chat_res.scalar_one_or_none()
        
        if not token or not chats:
            print("Telegram not configured: missing tg_token or tg_chats")
            return

        token_val = (token.value or "").strip()
        chat_list = [c.strip() for c in (chats.value or "").split(",") if c.strip()]
        if not token_val or not chat_list:
            print("Telegram not configured: empty tg_token or tg_chats")
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            for chat_id in chat_list:
                url = f"https://api.telegram.org/bot{token_val}/sendMessage"
                res = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": f"BEY CB BOT\n{message}"
                })
                if res.status_code >= 400:
                    print(f"Telegram send failed ({res.status_code}) chat_id={chat_id}: {res.text}")
    except Exception as e:
        print(f"Telegram Error: {e}")

# ── Auth Endpoints ───────────────────────────────────────────────────────────
class LoginData(BaseModel):
    username: str
    password: str

class ChangePasswordData(BaseModel):
    new_password: str

class EmployeeCreateData(BaseModel):
    username: str
    password: str

class EmployeeUpdateData(BaseModel):
    username: str

class EmployeeResetData(BaseModel):
    password: str

@app.post("/api/login")
async def login(data: LoginData, request: Request, db: AsyncSession = Depends(get_db)):
    username_row = await db.get(SettingsModel, "auth_username")
    password_row = await db.get(SettingsModel, "auth_password_hash")
    expected_user = username_row.value if username_row else DEFAULT_USERNAME
    expected_hash = password_row.value if password_row else hash_pw(DEFAULT_PASSWORD)

    employee_result = await db.execute(select(EmployeeModel).where(EmployeeModel.username == data.username))
    employee = employee_result.scalar_one_or_none()

    role = None
    if data.username == expected_user and hash_pw(data.password) == expected_hash:
        role = "admin"
    elif employee and hash_pw(data.password) == employee.password_hash:
        role = "employee"

    if role:
        token = secrets.token_urlsafe(32)
        sessions[token] = {"expiry": datetime.now() + SESSION_DURATION, "role": role}
        response = JSONResponse({"status": "success", "token": token, "role": role})
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=is_https_request(request),
            path="/"
        )
        return response
    return JSONResponse({"status": "error", "message": "Invalid username or password."}, status_code=401)

@app.post("/api/logout")
async def logout(request: Request):
    token = request_token(request)
    sessions.pop(token, None)
    response = JSONResponse({"status": "success"})
    response.delete_cookie("auth_token", path="/")
    return response

@app.get("/api/session")
async def session_status(request: Request):
    token = request_token(request)
    return {"status": "ok", "role": session_role(token)}

@app.post("/api/change-password")
async def change_password(data: ChangePasswordData, db: AsyncSession = Depends(get_db)):
    if not data.new_password or len(data.new_password) < 4:
        return JSONResponse({"error": "Password too short (min 4 chars)."}, status_code=400)
    new_hash = hash_pw(data.new_password)
    setting = await db.get(SettingsModel, "auth_password_hash")
    if setting:
        setting.value = new_hash
    else:
        db.add(SettingsModel(key="auth_password_hash", value=new_hash))
    await db.commit()
    return {"status": "success"}

@app.get("/api/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SettingsModel))
    settings = result.scalars().all()
    data = {s.key: s.value for s in settings}
    data.pop("auth_password_hash", None)
    data.pop("employee_password_hash", None)
    data.pop("employee_username", None)
    return data

@app.post("/api/settings")
async def save_settings(data: dict, db: AsyncSession = Depends(get_db)):
    data.pop("employee_username", None)
    data.pop("employee_password", None)
    for key, value in data.items():
        setting = await db.get(SettingsModel, key)
        if setting:
            setting.value = value
        else:
            db.add(SettingsModel(key=key, value=value))
    await db.commit()
    return {"status": "success"}

@app.get("/api/message-stats")
async def get_message_stats(db: AsyncSession = Depends(get_db)):
    stats = await load_message_stats(db, reset_day=True)
    return {"total": stats["total"], "today": stats["today"]}

@app.get("/api/employees")
async def get_employees(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EmployeeModel))
    employees = result.scalars().all()
    return [
        {"id": e.id, "username": e.username, "role": e.role or "employee"}
        for e in employees
    ]

@app.post("/api/employees")
async def create_employee(data: EmployeeCreateData, db: AsyncSession = Depends(get_db)):
    username = data.username.strip()
    if not username:
        return JSONResponse({"error": "Username required."}, status_code=400)
    if not data.password or len(data.password) < 4:
        return JSONResponse({"error": "Password too short (min 4 chars)."}, status_code=400)

    existing = await db.execute(select(EmployeeModel).where(EmployeeModel.username == username))
    if existing.scalar_one_or_none():
        return JSONResponse({"error": "Username already exists."}, status_code=409)

    employee = EmployeeModel(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_pw(data.password),
        role="employee"
    )
    db.add(employee)
    await db.commit()
    return {"status": "success", "employee": {"id": employee.id, "username": employee.username, "role": employee.role}}

@app.put("/api/employees/{employee_id}")
async def update_employee(employee_id: str, data: EmployeeUpdateData, db: AsyncSession = Depends(get_db)):
    employee = await db.get(EmployeeModel, employee_id)
    if not employee:
        return JSONResponse({"error": "Employee not found."}, status_code=404)

    username = data.username.strip()
    if not username:
        return JSONResponse({"error": "Username required."}, status_code=400)

    existing = await db.execute(select(EmployeeModel).where(EmployeeModel.username == username))
    existing_employee = existing.scalar_one_or_none()
    if existing_employee and existing_employee.id != employee_id:
        return JSONResponse({"error": "Username already exists."}, status_code=409)

    employee.username = username
    await db.commit()
    return {"status": "success"}

@app.post("/api/employees/{employee_id}/reset")
async def reset_employee_password(employee_id: str, data: EmployeeResetData, db: AsyncSession = Depends(get_db)):
    if not data.password or len(data.password) < 4:
        return JSONResponse({"error": "Password too short (min 4 chars)."}, status_code=400)
    employee = await db.get(EmployeeModel, employee_id)
    if not employee:
        return JSONResponse({"error": "Employee not found."}, status_code=404)
    employee.password_hash = hash_pw(data.password)
    await db.commit()
    return {"status": "success"}

@app.delete("/api/employees/{employee_id}")
async def delete_employee(employee_id: str, db: AsyncSession = Depends(get_db)):
    employee = await db.get(EmployeeModel, employee_id)
    if not employee:
        return JSONResponse({"error": "Employee not found."}, status_code=404)
    await db.delete(employee)
    await db.commit()
    return {"status": "success"}

@app.get("/api/accounts")
async def get_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AccountModel))
    accounts = result.scalars().all()
    return accounts

@app.post("/api/accounts/bulk-enabled")
async def bulk_enable_accounts(data: dict, db: AsyncSession = Depends(get_db)):
    enabled = parse_bool_setting(data.get("enabled"), True)
    await db.execute(update(AccountModel).values(enabled=enabled))
    await db.commit()
    return {"status": "success", "enabled": enabled}

@app.post("/api/accounts")
async def save_account(account: Account, db: AsyncSession = Depends(get_db)):
    if account.id:
        # Update existing
        db_account = await db.get(AccountModel, account.id)
        if db_account:
            db_account.username = account.username
            db_account.password = account.password
            db_account.proxies = account.proxies
            db_account.cookies = account.cookies
            db_account.enabled = True if account.enabled is None else account.enabled
        else:
            return {"status": "error", "message": "Account not found"}
    else:
        # Create new
        db_account = AccountModel(
            id=str(uuid.uuid4()),
            username=account.username,
            password=account.password,
            proxies=account.proxies,
            cookies=account.cookies,
            enabled=True if account.enabled is None else account.enabled
        )
        db.add(db_account)
        account.id = db_account.id
        
    await db.commit()
    return {"status": "success", "account": account}

@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, db: AsyncSession = Depends(get_db)):
    db_account = await db.get(AccountModel, account_id)
    if not db_account:
        return {"status": "error", "message": "Account not found"}
    
    await db.delete(db_account)
    await db.commit()
    return {"status": "success"}

@app.get("/api/targets")
async def get_targets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TargetModel))
    return result.scalars().all()

@app.post("/api/targets")
async def save_target(target: Target, db: AsyncSession = Depends(get_db)):
    if target.id:
        db_target = await db.get(TargetModel, target.id)
        if db_target:
            db_target.username = target.username
            db_target.description = target.description
            db_target.enabled = True if target.enabled is None else target.enabled
        else:
            return {"status": "error", "message": "Target not found"}
    else:
        db_target = TargetModel(
            id=str(uuid.uuid4()),
            username=target.username,
            description=target.description,
            enabled=True if target.enabled is None else target.enabled
        )
        db.add(db_target)
        target.id = db_target.id
    
    await db.commit()
    return {"status": "success", "target": target}

@app.delete("/api/targets/{target_id}")
async def delete_target(target_id: str, db: AsyncSession = Depends(get_db)):
    db_target = await db.get(TargetModel, target_id)
    if db_target:
        await db.delete(db_target)
        await db.commit()
        return {"status": "success"}
    return {"status": "error", "message": "Target not found"}

@sio.on('connect')
async def connect(sid, environ, auth=None):
    token = socket_auth_token(auth) or cookie_token_from_environ(environ)
    if not is_token_valid(token):
        return False
    socket_tokens[sid] = {"token": token, "role": session_role(token)}
    print(f"Client connected: {sid}")
    for name, procs in processes.items():
        running = bool(procs)
        if name in ("anon", "cookie"):
            running = is_anon_running()
        await sio.emit('status', {'name': name, 'status': 'running' if running else 'stopped'}, room=sid)
    await sio.emit('browser-count', build_browser_count_payload(), room=sid)

@sio.on('disconnect')
async def disconnect(sid):
    socket_tokens.pop(sid, None)

async def ensure_socket_auth(sid: str, required_role: Optional[str] = None) -> bool:
    entry = socket_tokens.get(sid) or {}
    token = entry.get("token", "")
    if not is_token_valid(token):
        await sio.disconnect(sid)
        return False
    if required_role and entry.get("role") != required_role:
        await sio.disconnect(sid)
        return False
    return True

async def start_anon_distributed() -> None:
    global distributed_run_active, distributed_run_id
    if not REDIS_ENABLED or not app.state.redis:
        await sio.emit('log', {'name': 'system', 'type': 'error',
            'data': 'Redis is not configured. Set REDIS_URL on the main server.'})
        return
    if distributed_run_active:
        await sio.emit('log', {'name': 'system', 'type': 'warning',
            'data': 'Distributed run already active. Stop it before starting again.'})
        return

    redis_conn = app.state.redis
    run_id = uuid.uuid4().hex
    distributed_run_active = True
    distributed_run_id = run_id

    try:
        if is_anon_running():
            await sio.emit('log', {'name': 'system', 'type': 'info',
                'data': 'Stopping existing bots before restart...'} )
            await publish_control(redis_conn, "stop", run_id="")
            await asyncio.sleep(2)

        cleanup_old_profiles()

        accounts = []
        targets = []
        target_username = ""
        started_count = 0
        skipped_count = 0
        cookie_headless = COOKIE_VIEWER_HEADLESS
        msg_enabled = False
        msg_min_minutes = 2
        msg_max_minutes = 5
        msg_texts_raw = ""

        async with SessionLocal() as db:
            acc_result = await db.execute(select(AccountModel))
            target_result = await db.execute(select(TargetModel))
            accounts = list(acc_result.scalars().all())
            targets = list(target_result.scalars().all())
            setting = await db.get(SettingsModel, "cookie_headless")
            cookie_headless = parse_bool_setting(
                setting.value if setting else None,
                COOKIE_VIEWER_HEADLESS,
            )

            msg_enabled_row = await db.get(SettingsModel, MESSAGE_ENABLED_KEY)
            msg_enabled = parse_bool_setting(msg_enabled_row.value if msg_enabled_row else None, False)
            msg_min_row = await db.get(SettingsModel, MESSAGE_MIN_MINUTES_KEY)
            msg_max_row = await db.get(SettingsModel, MESSAGE_MAX_MINUTES_KEY)
            msg_min_minutes = parse_int_setting(msg_min_row.value if msg_min_row else None, 2, min_value=2)
            msg_max_minutes = parse_int_setting(msg_max_row.value if msg_max_row else None, 5, min_value=2)
            msg_texts_row = await db.get(SettingsModel, MESSAGE_TEXTS_KEY)
            msg_texts_raw = (msg_texts_row.value if msg_texts_row else "") or ""

        if msg_min_minutes > msg_max_minutes:
            msg_min_minutes, msg_max_minutes = msg_max_minutes, msg_min_minutes

        msg_texts = [line.strip() for line in msg_texts_raw.splitlines() if line.strip()]
        msg_min_seconds = max(1, msg_min_minutes) * 60
        msg_max_seconds = max(msg_min_seconds, msg_max_minutes * 60)
        msg_enabled = bool(msg_enabled and msg_texts)

        if not accounts:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No accounts found! Add accounts in the IDs Manager tab.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        if not targets:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No target model defined! Add one in the Target Models tab.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        enabled_targets = [t for t in targets if normalize_enabled_value(t.enabled)]
        if not enabled_targets:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'All target models are disabled. Enable one to start.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        target_username = enabled_targets[0].username

        workers = await fetch_active_workers(redis_conn)
        if not workers:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No active workers found. Start worker.py on your servers.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        worker_slots: Dict[str, int] = {}
        total_capacity = 0
        for worker in workers:
            worker_id = str(worker.get("id") or "").strip()
            if not worker_id:
                continue
            max_profiles = parse_int_setting(worker.get("max_profiles"), 0, min_value=0)
            running = parse_int_setting(worker.get("running"), 0, min_value=0)
            effective_max = max_profiles if max_profiles > 0 else MAX_PROFILES_PER_SERVER
            per_worker_cap = min(effective_max, MAX_PROFILES_PER_SERVER)
            free_slots = max(0, per_worker_cap - running)
            worker_slots[worker_id] = free_slots
            total_capacity += free_slots

        if total_capacity <= 0:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'Workers are online but have no free slots. Stop existing bots first.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        launchable_accounts = []
        for acc in accounts:
            if not normalize_enabled_value(acc.enabled):
                skipped_count += 1
                continue
            if not acc.cookies:
                await sio.emit('log', {'name': 'system', 'type': 'warning',
                    'data': f'Skipping {acc.username} - no cookies set.'})
                skipped_count += 1
                continue
            launchable_accounts.append(acc)

        if not launchable_accounts:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No enabled accounts with cookies. Enable at least one to start.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        if len(launchable_accounts) > total_capacity:
            overflow = len(launchable_accounts) - total_capacity
            skipped_count += overflow
            launchable_accounts = launchable_accounts[:total_capacity]

        tasks = []
        for acc in launchable_accounts:
            try:
                cookies_obj = json.loads(acc.cookies)
                if isinstance(cookies_obj, dict) and "cookies" in cookies_obj:
                    cookies_obj = cookies_obj["cookies"]
                if not isinstance(cookies_obj, list):
                    raise ValueError("Invalid cookies payload")
            except Exception:
                await sio.emit('log', {'name': 'system', 'type': 'warning',
                    'data': f'Skipping {acc.username} - invalid cookies JSON.'})
                skipped_count += 1
                continue

            proxy = ""
            if acc.proxies and acc.proxies.strip():
                proxy = acc.proxies.splitlines()[0].strip()

            tasks.append({
                "run_id": run_id,
                "account_id": acc.id,
                "account_username": acc.username,
                "cookies": cookies_obj,
                "proxy": proxy,
                "target_username": target_username,
                "headless": cookie_headless,
                "msg_enabled": msg_enabled,
                "msg_min_seconds": msg_min_seconds,
                "msg_max_seconds": msg_max_seconds,
                "msg_texts": msg_texts,
            })

        if not tasks:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No valid accounts available to launch.'})
            distributed_run_active = False
            distributed_run_id = None
            return

        worker_ids = [wid for wid, slots in worker_slots.items() if slots > 0]
        worker_ids.sort()
        queue_map: Dict[str, list] = {wid: [] for wid in worker_ids}
        worker_index = 0
        for task in tasks:
            if not worker_ids:
                break
            attempts = 0
            while attempts < len(worker_ids) and worker_slots[worker_ids[worker_index]] <= 0:
                worker_index = (worker_index + 1) % len(worker_ids)
                attempts += 1
            if worker_slots[worker_ids[worker_index]] <= 0:
                break
            target_worker = worker_ids[worker_index]
            queue_map[target_worker].append(task)
            worker_slots[target_worker] -= 1
            worker_index = (worker_index + 1) % len(worker_ids)

        queue_keys = [REDIS_QUEUE_KEY] + [f"{REDIS_QUEUE_KEY}:{wid}" for wid in worker_ids]
        await redis_conn.delete(*queue_keys)

        pipe = redis_conn.pipeline()
        for worker_id, items in queue_map.items():
            if not items:
                continue
            queue_key = f"{REDIS_QUEUE_KEY}:{worker_id}"
            batch = []
            for task in items:
                batch.append(json.dumps(task))
                if len(batch) >= 100:
                    pipe.rpush(queue_key, *batch)
                    batch = []
            if batch:
                pipe.rpush(queue_key, *batch)
        await pipe.execute()

        started_count = sum(len(items) for items in queue_map.values())
        await sio.emit('log', {'name': 'system', 'type': 'info',
            'data': f'Queued {started_count} browser(s) for {target_username} across {len(workers)} worker(s).'} )
        await sio.emit('status', {'name': 'anon',   'status': 'running'})
        await sio.emit('status', {'name': 'cookie', 'status': 'running'})

        async with SessionLocal() as db:
            await send_telegram(
                f"BEY CB BOT STARTED (DISTRIBUTED)\n"
                f"Target: {target_username}\n"
                f"Browsers queued: {started_count}\n"
                f"Skipped: {skipped_count}\n"
                f"Workers online: {len(workers)}",
                db
            )
    except Exception as exc:
        distributed_run_active = False
        distributed_run_id = None
        await sio.emit('log', {'name': 'system', 'type': 'error',
            'data': f'Distributed launch failed: {exc}'})

@sio.on('start-anon')
async def start_anon(sid, data):
    """Start one browser per account (cookie viewer)."""
    global launch_in_progress
    if not await ensure_socket_auth(sid, required_role="admin"):
        return
    if launch_in_progress:
        await sio.emit('log', {'name': 'system', 'type': 'warning',
            'data': 'Launch already in progress. Please wait for it to finish.'})
        return
    launch_in_progress = True
    try:
        if REDIS_ENABLED:
            await start_anon_distributed()
            return
        # Stop any existing processes before starting new ones to ensure a clean state
        if is_anon_running():
             await sio.emit('log', {'name': 'system', 'type': 'info', 'data': 'Stopping existing bots before restart...'})
             stop_process("anon")
             stop_process("cookie")
             await asyncio.sleep(2)
        global launch_cancel_event
        launch_cancel_event = asyncio.Event()

        cleanup_old_profiles()

        # ── Fetch data from DB ────────────────────────────────────────────────
        accounts        = []
        targets         = []
        target_username = ""
        started_count   = 0
        skipped_count   = 0
        cookie_headless = COOKIE_VIEWER_HEADLESS
        msg_enabled     = False
        msg_min_minutes = 2
        msg_max_minutes = 5
        msg_texts_raw   = ""

        async with SessionLocal() as db:
            acc_result    = await db.execute(select(AccountModel))
            target_result = await db.execute(select(TargetModel))
            accounts = list(acc_result.scalars().all())
            targets  = list(target_result.scalars().all())
            setting = await db.get(SettingsModel, "cookie_headless")
            cookie_headless = parse_bool_setting(
                setting.value if setting else None,
                COOKIE_VIEWER_HEADLESS,
            )

            msg_enabled_row = await db.get(SettingsModel, MESSAGE_ENABLED_KEY)
            msg_enabled = parse_bool_setting(msg_enabled_row.value if msg_enabled_row else None, False)
            msg_min_row = await db.get(SettingsModel, MESSAGE_MIN_MINUTES_KEY)
            msg_max_row = await db.get(SettingsModel, MESSAGE_MAX_MINUTES_KEY)
            msg_min_minutes = parse_int_setting(msg_min_row.value if msg_min_row else None, 2, min_value=2)
            msg_max_minutes = parse_int_setting(msg_max_row.value if msg_max_row else None, 5, min_value=2)
            msg_texts_row = await db.get(SettingsModel, MESSAGE_TEXTS_KEY)
            msg_texts_raw = (msg_texts_row.value if msg_texts_row else "") or ""

        if msg_min_minutes > msg_max_minutes:
            msg_min_minutes, msg_max_minutes = msg_max_minutes, msg_min_minutes

        msg_texts = [line.strip() for line in msg_texts_raw.splitlines() if line.strip()]
        msg_min_seconds = max(1, msg_min_minutes) * 60
        msg_max_seconds = max(msg_min_seconds, msg_max_minutes * 60)
        msg_enabled = bool(msg_enabled and msg_texts)

        if not accounts:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No accounts found! Add accounts in the IDs Manager tab.'})
            await sio.emit('status', {'name': 'anon',   'status': 'stopped'})
            await sio.emit('status', {'name': 'cookie', 'status': 'stopped'})
            return

        if not targets:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No target model defined! Add one in the Target Models tab.'})
            await sio.emit('status', {'name': 'anon',   'status': 'stopped'})
            await sio.emit('status', {'name': 'cookie', 'status': 'stopped'})
            return

        enabled_targets = [t for t in targets if normalize_enabled_value(t.enabled)]
        if not enabled_targets:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'All target models are disabled. Enable one to start.'})
            await sio.emit('status', {'name': 'anon',   'status': 'stopped'})
            await sio.emit('status', {'name': 'cookie', 'status': 'stopped'})
            return

        launchable_accounts = []
        for acc in accounts:
            if not normalize_enabled_value(acc.enabled):
                # Only log skipping if there was a reason to think it might run
                # To avoid log spam, we just silently skip truly disabled accounts
                skipped_count += 1
                continue
            if not acc.cookies:
                await sio.emit('log', {'name': 'system', 'type': 'warning',
                    'data': f'Skipping {acc.username} - no cookies set.'})
                skipped_count += 1
                continue
            launchable_accounts.append(acc)

        if not launchable_accounts:
            await sio.emit('log', {'name': 'system', 'type': 'error',
                'data': 'No enabled accounts with cookies. Enable at least one to start.'})
            await sio.emit('status', {'name': 'anon',   'status': 'stopped'})
            await sio.emit('status', {'name': 'cookie', 'status': 'stopped'})
            return

        if len(launchable_accounts) > MAX_PROFILES_PER_SERVER:
            overflow = len(launchable_accounts) - MAX_PROFILES_PER_SERVER
            skipped_count += overflow
            launchable_accounts = launchable_accounts[:MAX_PROFILES_PER_SERVER]
            await sio.emit('log', {'name': 'system', 'type': 'warning',
                'data': f'Local server cap is {MAX_PROFILES_PER_SERVER} browsers. Skipping {overflow} account(s).'} )

        target_username = enabled_targets[0].username
        enabled_count = len(launchable_accounts)
        await sio.emit('log', {'name': 'system', 'type': 'info',
            'data': f'Launching {enabled_count} browsers total for {target_username} (sequential batch mode).'})

        tile_total = len(launchable_accounts)

        # Get batch settings or use defaults
        batch_size_row = await db.get(SettingsModel, "launch_batch_size")
        batch_delay_row = await db.get(SettingsModel, "launch_batch_delay")
        instance_delay_row = await db.get(SettingsModel, "launch_instance_delay")

        batch_size = parse_int_setting(batch_size_row.value if batch_size_row else None, 3, min_value=1)
        batch_delay = parse_int_setting(batch_delay_row.value if batch_delay_row else None, 10, min_value=0)
        instance_delay = parse_int_setting(instance_delay_row.value if instance_delay_row else None, 2, min_value=0)

        tile_cols = 5

        os.makedirs('data/temp', exist_ok=True)

        # ── Launch one browser per account ────────────────────────────────────
        cancel_event = launch_cancel_event
        tile_index = 0

        async def launch_account(acc: AccountModel, slot_index: int) -> None:
            nonlocal started_count
            try:
                cookies_obj = json.loads(acc.cookies)
                if isinstance(cookies_obj, dict) and "cookies" in cookies_obj:
                    cookies_obj = cookies_obj["cookies"]

                cookie_path = f"data/temp/cookies_{acc.id}.json"
                with open(cookie_path, 'w') as fp:
                    json.dump(cookies_obj, fp)

                proxy = ""
                if acc.proxies and acc.proxies.strip():
                    proxy = acc.proxies.splitlines()[0].strip()

                attempt = 1
                launched = False
                while attempt <= MAX_LAUNCH_ATTEMPTS:
                    if cancel_event.is_set():
                        break
                    cmd = [
                        os.sys.executable,
                        "bots/cookieviewer.py",
                        target_username,
                        cookie_path
                    ]
                    if cookie_headless:
                        cmd.append("--headless")
                    if proxy:
                        cmd.extend(["--proxy", proxy])

                    await sio.emit('log', {'name': 'system', 'type': 'info',
                        'data': f'Launching browser for {acc.username} (attempt {attempt}/{MAX_LAUNCH_ATTEMPTS})...'})

                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        cwd=os.getcwd(),
                        text=True,
                        bufsize=1,
                        env={
                            **os.environ,
                            "PYTHONUNBUFFERED": "1",
                            "CB_MSG_ENABLED": "1" if msg_enabled else "0",
                            "CB_MSG_MIN_SECONDS": str(msg_min_seconds),
                            "CB_MSG_MAX_SECONDS": str(msg_max_seconds),
                            "CB_MSGS_JSON": json.dumps(msg_texts),
                            "CB_TILE_INDEX": str(slot_index),
                            "CB_TILE_TOTAL": str(tile_total),
                            "CB_TILE_COLS": str(tile_cols)
                        }
                    )
                    processes["cookie"].append(proc)
                    ready_event = asyncio.Event()
                    asyncio.create_task(stream_logs(
                        "cookie",
                        proc,
                        display_name=f"cookie:{acc.username}",
                        ready_event=ready_event,
                        ready_phrase=READY_LOG_PHRASE,
                        acc_id=acc.id,
                        slot_index=slot_index
                    ))

                    await sio.emit('log', {'name': 'system', 'type': 'info',
                        'data': f'Waiting for {acc.username} to reach target room before starting next browser...'})

                    ready = await wait_for_process_ready(proc, ready_event, READY_WAIT_TIMEOUT, cancel_event=cancel_event)
                    if cancel_event.is_set():
                        stop_single_process(proc)
                        break
                    if ready:
                        launched = True
                        started_count += 1
                        await sio.emit('log', {'name': 'system', 'type': 'info',
                            'data': f'{acc.username} reached target room. Continuing...'})
                        break

                    if proc.poll() is None:
                        launched = True
                        started_count += 1
                        await sio.emit('log', {'name': 'system', 'type': 'warning',
                            'data': f'{acc.username} still loading; continuing to next browser.'})
                        break

                    await sio.emit('log', {'name': 'system', 'type': 'error',
                        'data': f'Browser for {acc.username} exited before reaching target room.'})

                    attempt += 1
                    if attempt <= MAX_LAUNCH_ATTEMPTS:
                        await sio.emit('log', {'name': 'system', 'type': 'info',
                            'data': f'Retrying {acc.username}...'})

                if not launched and not cancel_event.is_set():
                    await sio.emit('log', {'name': 'system', 'type': 'error',
                        'data': f'Failed to launch browser for {acc.username} after {MAX_LAUNCH_ATTEMPTS} attempts. Continuing.'})

            except Exception as e:
                print(f"[start_anon] Error starting {acc.username}: {e}")
                await sio.emit('log', {'name': 'system', 'type': 'error',
                    'data': f'Error starting {acc.username}: {e}'})

        for i, batch_start in enumerate(range(0, len(launchable_accounts), batch_size)):
            if cancel_event.is_set():
                await sio.emit('log', {'name': 'system', 'type': 'warning',
                    'data': 'Launch cancelled by user.'})
                break

            if i > 0 and batch_delay > 0:
                await sio.emit('log', {'name': 'system', 'type': 'info',
                    'data': f'Waiting {batch_delay}s before next batch...'})
                await asyncio.sleep(batch_delay)

            batch = launchable_accounts[batch_start:batch_start + batch_size]
            batch_tasks = []
            for j, acc in enumerate(batch):
                if cancel_event.is_set():
                    break
                if j > 0 and instance_delay > 0:
                    await asyncio.sleep(instance_delay)

                slot_index = tile_index
                tile_index += 1
                batch_tasks.append(asyncio.create_task(launch_account(acc, slot_index)))

            # Await the completion of the launch_account tasks for THIS batch
            # This ensures they have reached the target room or timed out before we move to the next batch.
            if batch_tasks:
                await asyncio.gather(*batch_tasks)

        if not cancel_event.is_set():
            # ── Summary ───────────────────────────────────────────────────────────
            await sio.emit('log', {'name': 'system', 'type': 'info',
                'data': f'Done! {started_count} browser(s) launched, {skipped_count} skipped.'})
            await sio.emit('browser-count', build_browser_count_payload())
            await sio.emit('status', {'name': 'anon',   'status': 'running'})
            await sio.emit('status', {'name': 'cookie', 'status': 'running'})

            # ── Telegram notification ─────────────────────────────────────────────
            async with SessionLocal() as db:
                await send_telegram(
                    f"BEY CB BOT STARTED\n"
                    f"Target: {target_username}\n"
                    f"Browsers launched: {started_count}\n"
                    f"Skipped (no cookies): {skipped_count}\n"
                    f"Total accounts: {len(accounts)}",
                    db
                )

    except Exception as e:
        print(f"[start_anon] FATAL: {e}")
        import traceback; traceback.print_exc()
        await sio.emit('log', {'name': 'system', 'type': 'error',
            'data': f'Fatal error starting bots: {e}'})
    finally:
        launch_in_progress = False

@sio.on('stop-anon')
async def stop_anon(sid, data=None):
    if not await ensure_socket_auth(sid, required_role="admin"):
        return
    global launch_cancel_event, distributed_run_active, distributed_run_id
    if launch_cancel_event:
        launch_cancel_event.set()
    if REDIS_ENABLED and app.state.redis:
        await publish_control(app.state.redis, "stop", run_id=distributed_run_id or "")
        workers = await fetch_active_workers(app.state.redis)
        worker_ids = [str(w.get("id") or "").strip() for w in workers]
        queue_keys = [REDIS_QUEUE_KEY] + [f"{REDIS_QUEUE_KEY}:{wid}" for wid in worker_ids if wid]
        await app.state.redis.delete(*queue_keys)
        distributed_run_active = False
        distributed_run_id = None
        worker_browser_counts.clear()
        worker_browser_platforms.clear()
    stop_process("anon")
    stop_process("cookie")
    async with SessionLocal() as db:
        await send_telegram("BEY CB BOT STOPPED\nAll browser instances closed.", db)

# Static files (mount at root for simplicity)
# Must be after all other routes
app.mount("/", StaticFiles(directory="public", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="0.0.0.0", port=3000)
