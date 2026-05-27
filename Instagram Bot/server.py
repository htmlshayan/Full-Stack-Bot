"""
Flask server — runs the DM bot continuously until manually stopped.
Provides a live dashboard to monitor status.
"""
import sys
import os
import json
import threading
import logging
import re
from functools import wraps
from datetime import datetime, timedelta
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env_file(path: str, override: bool = False) -> bool:
  env_path = str(path or "").strip()
  if not env_path or not os.path.exists(env_path):
    return False

  try:
    with open(env_path, "r", encoding="utf-8") as handle:
      lines = handle.read().splitlines()
  except Exception:
    return False

  for raw_line in lines:
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue

    if line.lower().startswith("export "):
      line = line[7:].strip()

    if "=" not in line:
      continue

    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
      continue

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
      value = value[1:-1]

    if override or key not in os.environ:
      os.environ[key] = value

  return True


_load_env_file(ENV_PATH)

from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from werkzeug.utils import secure_filename
from bot import run_bot, setup_logging, force_stop_active_sessions
from config import database
from config.database import get_setting
from telegram.bot import telegram_bot

# ── Config ──
BOT_LOOP_ENABLED = True
MAX_PROXIES_PER_ACCOUNT = 5
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
ALLOWED_REPLY_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "beyinstabot-local-secret")
app.jinja_env.auto_reload = True
logger = logging.getLogger("model_dm_bot")
database.init_db()

# ── Shared State ──
dm_bot_state = {
    "status": "idle",           # idle | running | stopping | stopped
    "mode": "dm",
    "current_session": 0,
    "total_sessions": 0,
    "last_run_start": None,
    "last_run_end": None,
    "next_run": None,
    "total_dms_all_time": 0,
    "dms_sent_today": 0,
    "last_dm_sent_at": "",
    "started_by": "",
    "started_by_role": "employee",
    "errors": [],
    "log_lines": [],
}


# Legacy bot_state for broad compatibility, but engines now use the above
bot_state = dm_bot_state

dm_bot_thread = None
dm_bot_thread_lock = threading.Lock()
dm_stop_event = threading.Event()

liker_bot_state = {
    "status": "idle",
    "mode": "liker",
    "total_likes_all_time": 0,
    "likes_today": 0,
    "last_like_at": "",
    "log_lines": [],
}
liker_bot_thread = None
liker_bot_thread_lock = threading.Lock()
liker_stop_event = threading.Event()

story_liker_bot_state = {
  "status": "idle",
  "mode": "story",
  "total_story_likes_all_time": 0,
  "story_likes_today": 0,
  "last_story_like_at": "",
  "log_lines": [],
}
story_liker_bot_thread = None
story_liker_bot_thread_lock = threading.Lock()
story_liker_stop_event = threading.Event()



cluster_control_thread = None
cluster_control_thread_lock = threading.Lock()
cluster_control_stop_event = threading.Event()
cluster_control_last_nonce = ""
_last_nonce = ""

CLUSTER_CONTROL_SETTING_KEY = "BOT_CLUSTER_CONTROL"


def _env_is_true(name: str, default: bool = False) -> bool:
  raw = os.environ.get(name)
  if raw is None:
    return bool(default)

  text = str(raw).strip().lower()
  if text in ("1", "true", "yes", "on", "enable", "enabled"):
    return True
  if text in ("0", "false", "no", "off", "disable", "disabled"):
    return False

  return bool(default)


def _normalize_role(raw_role: str, default: str = "employee") -> str:
  role = str(raw_role or "").strip().lower()
  if role in ("master", "employee"):
    return role

  fallback = str(default or "employee").strip().lower()
  if fallback in ("master", "employee"):
    return fallback
  return "employee"


def _normalize_cluster_state(raw_state: str) -> str:
  state = str(raw_state or "").strip().lower()
  if state in ("start", "run", "running", "resume", "on", "1", "true"):
    return "running"
  if state in ("stop", "stopped", "idle", "off", "0", "false", "pause"):
    return "stopped"
  return ""


def _normalize_run_mode(raw_mode: str) -> str:
  mode = str(raw_mode or "").strip().lower()
  if mode in ("dm", "liker", "story"):
    return mode
  if mode in ("all", "both", "dm+liker", "liker+dm", "dm+story", "story+dm", "liker+story", "story+liker"):
    return "all"
  return ""





def _build_cluster_control_payload(
  desired_state: str,
  issued_by: str = "",
  issued_by_role: str = "employee",
  run_mode: str = "dm",
) -> dict:
  normalized_state = _normalize_cluster_state(desired_state)
  if not normalized_state:
    raise ValueError(f"Invalid cluster control state: {desired_state}")

  normalized_mode = _normalize_run_mode(run_mode) or "dm"

  return {
    "desired_state": normalized_state,
    "run_mode": normalized_mode,
    "issued_by": str(issued_by or "").strip().lower(),
    "issued_by_role": _normalize_role(issued_by_role, default="employee"),
    "issued_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    "nonce": uuid4().hex,
  }


def _get_cluster_control_payload():
  raw_payload = get_setting(CLUSTER_CONTROL_SETTING_KEY, None)
  if not isinstance(raw_payload, dict):
    return None

  desired_state = _normalize_cluster_state(raw_payload.get("desired_state", ""))
  if not desired_state:
    return None

  run_mode = _normalize_run_mode(raw_payload.get("run_mode", "")) or "dm"

  issued_by = str(raw_payload.get("issued_by", "") or "").strip().lower()
  issued_by_role = _normalize_role(raw_payload.get("issued_by_role", "employee"), default="employee")
  issued_at = str(raw_payload.get("issued_at", "") or "").strip()
  nonce = str(raw_payload.get("nonce", "") or "").strip() or f"legacy-{desired_state}"

  return {
    "desired_state": desired_state,
    "run_mode": run_mode,
    "issued_by": issued_by,
    "issued_by_role": issued_by_role,
    "issued_at": issued_at,
    "nonce": nonce,
  }


def _publish_cluster_control(
  desired_state: str,
  issued_by: str = "",
  issued_by_role: str = "employee",
  run_mode: str = "dm",
) -> dict:
  payload = _build_cluster_control_payload(
    desired_state=desired_state,
    issued_by=issued_by,
    issued_by_role=issued_by_role,
    run_mode=run_mode,
  )
  database.save_settings({CLUSTER_CONTROL_SETTING_KEY: payload})
  return payload


def _is_bot_running() -> bool:
  with dm_bot_thread_lock:
    running_dm = bool(dm_bot_thread and dm_bot_thread.is_alive())
  return running_dm


def _is_liker_running() -> bool:
  with liker_bot_thread_lock:
    running_liker = bool(liker_bot_thread and liker_bot_thread.is_alive())
  return running_liker


def _is_story_liker_running() -> bool:
  with story_liker_bot_thread_lock:
    running_story = bool(story_liker_bot_thread and story_liker_bot_thread.is_alive())
  return running_story


def _request_local_stop(mode: str = "all") -> bool:
  was_running = False
  
  # Stop DM
  if mode in ("dm", "all"):
      dm_stop_event.set()
      with dm_bot_thread_lock:
          if dm_bot_thread and dm_bot_thread.is_alive():
              was_running = True
              dm_bot_state["status"] = "stopping"
          else:
              dm_bot_state["status"] = "stopped"

  # Stop Liker
  if mode in ("liker", "all"):
    liker_stop_event.set()
    with liker_bot_thread_lock:
      if liker_bot_thread and liker_bot_thread.is_alive():
        was_running = True
        liker_bot_state["status"] = "stopping"
      else:
        liker_bot_state["status"] = "stopped"

  # Stop Story Liker
  if mode in ("story", "all"):
    story_liker_stop_event.set()
    with story_liker_bot_thread_lock:
      if story_liker_bot_thread and story_liker_bot_thread.is_alive():
        was_running = True
        story_liker_bot_state["status"] = "stopping"
      else:
        story_liker_bot_state["status"] = "stopped"


          
  force_stop_active_sessions()
  return was_running


def _cluster_control_poll_seconds() -> float:
  raw_value = os.environ.get("BOT_CLUSTER_CONTROL_POLL_SEC", "2")
  try:
    poll_seconds = float(raw_value)
  except Exception:
    poll_seconds = 2.0

  return max(0.5, min(30.0, poll_seconds))


def _cluster_control_loop():
  global cluster_control_last_nonce

  poll_seconds = _cluster_control_poll_seconds()
  logger.info(f"Cluster control watcher started (poll every {poll_seconds:.1f}s).")

  while not cluster_control_stop_event.is_set():
    try:
      control = _get_cluster_control_payload()
      if control:
        desired_state = control["desired_state"]
        run_mode = _normalize_run_mode(control.get("run_mode", "")) or "dm"
        nonce = control["nonce"]

        if nonce != cluster_control_last_nonce:
          cluster_control_last_nonce = nonce
          issued_by = control.get("issued_by", "")
          issued_by_role = control.get("issued_by_role", "employee")
          logger.info(
            f"Received cluster command '{desired_state}' from @{issued_by or 'unknown'} ({issued_by_role})."
          )

        if desired_state == "running":
          if run_mode in ("dm", "all"):
            if not _is_bot_running():
              started = _start_bot_loop(
                  started_by=issued_by,
                  started_by_role=issued_by_role
              )
              if started:
                logger.info("Applied cluster start command on this node (dm).")
          if run_mode in ("liker", "all"):
            if not _is_liker_running():
              started = _start_liker_loop(
                  started_by=issued_by,
                  started_by_role=issued_by_role,
              )
              if started:
                logger.info("Applied cluster start command on this node (liker).")
          if run_mode in ("story", "all"):
            if not _is_story_liker_running():
              started = _start_story_liker_loop(
                  started_by=issued_by,
                  started_by_role=issued_by_role,
              )
              if started:
                logger.info("Applied cluster start command on this node (story).")
        elif desired_state == "stopped":
          if (
            _is_bot_running()
            or _is_liker_running()
            or _is_story_liker_running()
            or bot_state.get("status") in ("running", "stopping")
            or liker_bot_state.get("status") in ("running", "stopping")
            or story_liker_bot_state.get("status") in ("running", "stopping")
          ):
            _request_local_stop(mode=run_mode)
      
    except Exception as e:
      logger.debug(f"Cluster control watcher error: {e}")

    cluster_control_stop_event.wait(poll_seconds)


def _ensure_cluster_control_watcher():
  global cluster_control_thread

  with cluster_control_thread_lock:
    if cluster_control_thread and cluster_control_thread.is_alive():
      return

    cluster_control_stop_event.clear()
    cluster_control_thread = threading.Thread(
      target=_cluster_control_loop,
      name="cluster-control-watcher",
      daemon=True,
    )
    cluster_control_thread.start()


def _start_bot_loop(
  started_by: str = "",
  started_by_role: str = "employee",
) -> bool:
  """Start bot loop thread; return False when already running."""
  global dm_bot_thread

  thread_lock = dm_bot_thread_lock
  stop_evt = dm_stop_event
  state_obj = dm_bot_state
  target_func = bot_loop

  with thread_lock:
    if dm_bot_thread and dm_bot_thread.is_alive():
      return False

    stop_evt.clear()
    state_obj["started_by"] = str(started_by or "").strip().lower()
    state_obj["started_by_role"] = _normalize_role(started_by_role, default="employee")
    state_obj["mode"] = "dm"
    state_obj["status"] = "running"

    new_thread = threading.Thread(target=target_func, daemon=True)
    dm_bot_thread = new_thread
    
    new_thread.start()
    return True


def _start_liker_loop(
  started_by: str = "",
  started_by_role: str = "employee",
) -> bool:
  """Start liker loop thread; return False when already running."""
  global liker_bot_thread

  with liker_bot_thread_lock:
    if liker_bot_thread and liker_bot_thread.is_alive():
      return False

    liker_stop_event.clear()
    liker_bot_state["mode"] = "liker"
    liker_bot_state["status"] = "running"

    normalized_role = _normalize_role(started_by_role, default="employee")
    owner = str(started_by or "").strip().lower() if normalized_role == "employee" else None

    liker_bot_thread = threading.Thread(target=liker_loop, args=(owner,), daemon=True)
    liker_bot_thread.start()
    return True


def _start_story_liker_loop(
  started_by: str = "",
  started_by_role: str = "employee",
) -> bool:
  """Start story liker loop thread; return False when already running."""
  global story_liker_bot_thread

  with story_liker_bot_thread_lock:
    if story_liker_bot_thread and story_liker_bot_thread.is_alive():
      return False

    story_liker_stop_event.clear()
    story_liker_bot_state["mode"] = "story"
    story_liker_bot_state["status"] = "running"

    normalized_role = _normalize_role(started_by_role, default="employee")
    owner = str(started_by or "").strip().lower() if normalized_role == "employee" else None

    story_liker_bot_thread = threading.Thread(target=story_liker_loop, args=(owner,), daemon=True)
    story_liker_bot_thread.start()
    return True


# ── Authentication ──
# (Auth DB is now handled natively by config.database)


def login_required(route_func):
  @wraps(route_func)
  def wrapper(*args, **kwargs):
    if session.get("authenticated"):
      return route_func(*args, **kwargs)

    if request.path.startswith("/api/"):
      return jsonify({"success": False, "error": "Unauthorized"}), 401

    return redirect(url_for("login"))

  return wrapper


def _current_user_context():
  return {
    "username": session.get("username", ""),
    "role": session.get("role", "employee"),
  }


def _is_master():
  return session.get("role") == "master"


def master_required(route_func):
  @wraps(route_func)
  def wrapper(*args, **kwargs):
    if _is_master():
      return route_func(*args, **kwargs)

    if request.path.startswith("/api/"):
      return jsonify({"success": False, "error": "Master access required"}), 403

    return redirect(url_for("dashboard"))

  return wrapper


def _log_actor_action(action, target_type="", target_value="", details=None, employees_only=True):
  """Append an actor action to audit log. By default only logs employee actions."""
  user_ctx = _current_user_context()
  actor_username = user_ctx.get("username", "")
  actor_role = user_ctx.get("role", "employee")

  if not actor_username:
    return
  if employees_only and actor_role != "employee":
    return

  try:
    database.log_activity(
      actor_username,
      actor_role,
      action,
      target_type=target_type,
      target_value=target_value,
      details=details,
    )
  except Exception as e:
    logger.debug(f"Activity log failed for {actor_username}: {e}")


def _setting_int(key: str) -> int:
  value = get_setting(key)
  if value is None:
    raise KeyError(f"Missing required setting in database: {key}")
  try:
    return int(value)
  except (TypeError, ValueError):
    raise ValueError(f"Invalid integer setting '{key}': {value}")


def _refresh_total_dms_all_time():
  """Refresh dashboard DM metrics from DB for live dashboard/status reporting."""
  try:
    metrics = database.get_dm_dashboard_metrics()
    bot_state["total_dms_all_time"] = int(metrics.get("lifetime_total_sent", 0) or 0)
    bot_state["dms_sent_today"] = int(metrics.get("dms_sent_today", 0) or 0)
    bot_state["last_dm_sent_at"] = str(metrics.get("last_dm_sent_at", "") or "")
    

  except Exception as e:
    logger.debug(f"Failed to refresh DM dashboard metrics: {e}")


def _refresh_total_likes():
  """Refresh dashboard Like metrics from DB."""
  try:
    metrics = database.get_liker_dashboard_metrics()
    db_total = int(metrics.get("lifetime_likes", 0) or 0)
    db_today = int(metrics.get("likes_today", 0) or 0)
    db_last_like = str(metrics.get("last_like_at", "") or "")

    current_total = int(liker_bot_state.get("total_likes_all_time", 0) or 0)
    current_today = int(liker_bot_state.get("likes_today", 0) or 0)
    current_last_like = str(liker_bot_state.get("last_like_at", "") or "")

    liker_bot_state["total_likes_all_time"] = max(db_total, current_total)
    liker_bot_state["likes_today"] = max(db_today, current_today)
    liker_bot_state["last_like_at"] = db_last_like or current_last_like
  except Exception as e:
    logger.debug(f"Failed to refresh Like dashboard metrics: {e}")


def _refresh_total_story_likes():
  """Refresh dashboard Story Like metrics from DB."""
  try:
    metrics = database.get_story_liker_dashboard_metrics()
    db_total = int(metrics.get("lifetime_story_likes", 0) or 0)
    db_today = int(metrics.get("story_likes_today", 0) or 0)
    db_last_like = str(metrics.get("last_story_like_at", "") or "")

    current_total = int(story_liker_bot_state.get("total_story_likes_all_time", 0) or 0)
    current_today = int(story_liker_bot_state.get("story_likes_today", 0) or 0)
    current_last_like = str(story_liker_bot_state.get("last_story_like_at", "") or "")

    story_liker_bot_state["total_story_likes_all_time"] = max(db_total, current_total)
    story_liker_bot_state["story_likes_today"] = max(db_today, current_today)
    story_liker_bot_state["last_story_like_at"] = db_last_like or current_last_like
  except Exception as e:
    logger.debug(f"Failed to refresh Story Like dashboard metrics: {e}")



def _ensure_telegram_polling():
  """Keep Telegram command polling available even when automation is idle."""
  try:
    telegram_bot.start_polling()
  except Exception as e:
    logger.debug(f"Failed to ensure Telegram polling: {e}")


_refresh_total_dms_all_time()
_refresh_total_likes()
_refresh_total_story_likes()
_ensure_telegram_polling()


def _normalize_text_list(raw_items):
  if not isinstance(raw_items, list):
    return []

  clean = []
  for item in raw_items:
    if not isinstance(item, str):
      continue
    text = item.strip()
    if text:
      clean.append(text)
  return clean


def _normalize_bool_flag(raw_value, default: bool = True) -> bool:
  if isinstance(raw_value, bool):
    return raw_value
  if raw_value is None:
    return bool(default)
  if isinstance(raw_value, (int, float)):
    return int(raw_value) != 0

  text = str(raw_value).strip().lower()
  if text in ("", "none", "null"):
    return bool(default)
  if text in ("1", "true", "on", "yes", "enable", "enabled"):
    return True
  if text in ("0", "false", "off", "no", "disable", "disabled"):
    return False
  return bool(default)


def _split_proxy_entries(raw_proxy):
  text = str(raw_proxy or "")
  if not text.strip():
    return []

  clean = []
  seen = set()
  for part in re.split(r"[\r\n,;]+", text):
    proxy = str(part or "").strip()
    if not proxy:
      continue

    key = proxy.lower()
    if key in seen:
      continue
    seen.add(key)
    clean.append(proxy)

  return clean


def _normalize_proxy_value(raw_proxy, max_items: int = MAX_PROXIES_PER_ACCOUNT):
  proxy_entries = _split_proxy_entries(raw_proxy)
  too_many = len(proxy_entries) > max_items
  limited = proxy_entries[:max_items]
  return ", ".join(limited), too_many, len(proxy_entries)


def _mask_single_proxy_for_view(proxy_value: str):
  clean = str(proxy_value or "").strip()
  if not clean:
    return ""

  if "@" not in clean:
    return clean

  if "://" in clean:
    scheme, rest = clean.split("://", 1)
    prefix = f"{scheme}://"
  else:
    rest = clean
    prefix = ""

  creds, host = rest.rsplit("@", 1)
  if ":" in creds:
    username = creds.split(":", 1)[0]
    safe_creds = f"{username}:***"
  else:
    safe_creds = "***"

  return f"{prefix}{safe_creds}@{host}"


def _mask_proxy_for_view(proxy_value):
  """Mask proxy credentials for read-only queue display."""
  proxy_entries = _split_proxy_entries(proxy_value)
  if not proxy_entries:
    return ""

  masked = [_mask_single_proxy_for_view(proxy) for proxy in proxy_entries[:MAX_PROXIES_PER_ACCOUNT]]
  hidden_count = max(0, len(proxy_entries) - MAX_PROXIES_PER_ACCOUNT)
  if hidden_count:
    masked.append(f"+{hidden_count} more")
  return ", ".join(masked)


# ── Dashboard HTML ──
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Instagram DM Bot — Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 2rem;
  }
  .container { max-width: 900px; margin: 0 auto; }
  h1 {
    text-align: center;
    font-size: 2rem;
    background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 2rem;
  }
  .status-bar {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .status-badge {
    padding: 0.5rem 1.5rem;
    border-radius: 50px;
    font-weight: 700;
    font-size: 0.9rem;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .status-idle { background: #1a1a2e; color: #888; border: 1px solid #333; }
  .status-running { background: #0d3320; color: #4ade80; border: 1px solid #166534; animation: pulse 2s infinite; }
  .status-cooldown { background: #1e1b3a; color: #a78bfa; border: 1px solid #4c1d95; }
  .status-stopped { background: #2d1215; color: #f87171; border: 1px solid #7f1d1d; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.7; } }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .card {
    background: #12121a;
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    padding: 1.5rem;
    text-align: center;
  }
  .card .value {
    font-size: 2rem;
    font-weight: 800;
    color: #fff;
    margin-bottom: 0.3rem;
  }
  .card .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }
  .logs {
    background: #0d0d14;
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    padding: 1.5rem;
    max-height: 400px;
    overflow-y: auto;
    font-family: 'Cascadia Code', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.6;
  }
  .logs .line { color: #6b7280; }
  .logs .line.info { color: #9ca3af; }
  .logs .line.success { color: #4ade80; }
  .logs .line.error { color: #f87171; }
  .logs .line.warn { color: #fbbf24; }
  .time-info {
    text-align: center;
    color: #555;
    font-size: 0.85rem;
    margin-bottom: 1.5rem;
  }
  .controls {
    display: flex;
    justify-content: center;
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .btn {
    padding: 0.6rem 2rem;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
  }
  .btn-stop { background: #7f1d1d; color: #fca5a5; }
  .btn-stop:hover { background: #991b1b; }
  .btn-start { background: #14532d; color: #86efac; }
  .btn-start:hover { background: #166534; }
</style>
</head>
<body>
<div class="container">
  <h1>🤖 Instagram DM Bot</h1>

  <div class="status-bar">
    <span class="status-badge status-{{ state.status }}">{{ state.status }}</span>
  </div>

  <div class="time-info">
    {% if state.next_run and state.status == 'cooldown' %}
      ⏳ Next run in: <strong>{{ state.next_run }}</strong>
    {% elif state.last_run_end %}
      Last completed: {{ state.last_run_end }}
    {% else %}
      Waiting to start...
    {% endif %}
  </div>

  <div class="controls">
    {% if state.status == 'stopped' %}
      <a href="/start"><button class="btn btn-start">▶ Start Loop</button></a>
    {% else %}
      <a href="/stop"><button class="btn btn-stop">■ Stop</button></a>
    {% endif %}
  </div>

  <div class="grid">
    <div class="card">
      <div class="value">{{ state.total_sessions }}</div>
      <div class="label">Sessions Run</div>
    </div>
    <div class="card">
      <div class="value">{{ state.total_dms_all_time }}</div>
      <div class="label">Total DMs Sent</div>
    </div>
    <div class="card">
      <div class="value">{{ cooldown_range }}</div>
      <div class="label">Cooldown</div>
    </div>
    <div class="card">
      <div class="value">{{ dm_log_count }}</div>
      <div class="label">Users Reached</div>
    </div>
  </div>

  <h2 style="margin-bottom:1rem;font-size:1rem;color:#888;">📜 Recent Logs</h2>
  <div class="logs">
    {% for line in state.log_lines[-50:]|reverse %}
      <div class="line {% if '✅' in line or 'successful' in line %}success{% elif '❌' in line or 'ERROR' in line %}error{% elif '⚠️' in line or 'WARNING' in line %}warn{% else %}info{% endif %}">{{ line }}</div>
    {% endfor %}
    {% if not state.log_lines %}
      <div class="line">No logs yet. Start the bot to see activity.</div>
    {% endif %}
  </div>
</div>
</body>
</html>
"""


# ── Log Capture Handler ──
class DashboardLogHandler(logging.Handler):
    """Captures log lines into bot_state for the dashboard."""
    def emit(self, record):
        msg = self.format(record)
        bot_state["log_lines"].append(msg)
        liker_bot_state["log_lines"].append(msg)
        story_liker_bot_state["log_lines"].append(msg)
        # Keep only last 200 lines
        if len(bot_state["log_lines"]) > 200:
            bot_state["log_lines"] = bot_state["log_lines"][-200:]
        if len(liker_bot_state["log_lines"]) > 200:
            liker_bot_state["log_lines"] = liker_bot_state["log_lines"][-200:]
        if len(story_liker_bot_state["log_lines"]) > 200:
            story_liker_bot_state["log_lines"] = story_liker_bot_state["log_lines"][-200:]


def _ensure_dashboard_log_handler():
    """Attach a single dashboard log handler instance to avoid duplicate log lines."""
    model_logger = logging.getLogger("model_dm_bot")
    for handler in model_logger.handlers:
        if isinstance(handler, DashboardLogHandler):
            return

    dash_handler = DashboardLogHandler()
    dash_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    model_logger.addHandler(dash_handler)


# ── Bot Loop ──
def bot_loop():
    """Run the bot continuously."""
    global dm_bot_state

    state_obj = dm_bot_state
    stop_evt = dm_stop_event

    setup_logging()
    _ensure_dashboard_log_handler()

    pass_num = 0

    while not stop_evt.is_set():
        pass_num += 1
        state_obj["status"] = "running"
        state_obj["current_session"] = pass_num
        state_obj["last_run_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state_obj["next_run"] = None

        logger.info(f"♻️ CONTINUOUS DM PASS #{pass_num} STARTING")

        started_by = state_obj.get("started_by", "")
        started_by_role = state_obj.get("started_by_role", "employee")
        account_owner = None if started_by_role == "master" else started_by
        
        try:
          run_bot(stop_event=stop_evt, account_owner=account_owner, continuous_mode=True)
        except Exception as e:
            logger.error(f"Continuous DM pass #{pass_num} crashed: {e}")
            state_obj["errors"].append(f"Pass {pass_num}: {e}")

        state_obj["total_sessions"] = pass_num
        state_obj["last_run_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _refresh_total_dms_all_time()

        if stop_evt.is_set():
            break

        if stop_evt.wait(1.0):
            break

    state_obj["status"] = "stopped"
    state_obj["next_run"] = None
    state_obj["started_by"] = ""
    state_obj["started_by_role"] = "employee"
    state_obj["mode"] = "dm"
    logger.info("🛑 DM Bot runner stopped.")


def liker_loop(account_owner=None):
    """Run the liker bot continuously."""
    global liker_bot_state

    state_obj = liker_bot_state
    stop_evt = liker_stop_event

    setup_logging()
    _ensure_dashboard_log_handler()

    pass_num = 0

    while not stop_evt.is_set():
        pass_num += 1
        state_obj["status"] = "running"
        state_obj["last_run_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"♻️ CONTINUOUS LIKER PASS #{pass_num} STARTING")

        from bot import run_liker_bot
        try:
            run_liker_bot(stop_event=stop_evt, account_owner=account_owner, continuous_mode=True, state_obj=state_obj)
        except Exception as e:
            logger.error(f"Continuous Liker pass #{pass_num} crashed: {e}")

        state_obj["last_run_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _refresh_total_likes()

        if stop_evt.is_set(): break
        if stop_evt.wait(1.0): break

    state_obj["status"] = "stopped"
    logger.info("🛑 Liker Bot runner stopped.")


def story_liker_loop(account_owner=None):
    """Run the story liker bot continuously."""
    global story_liker_bot_state

    state_obj = story_liker_bot_state
    stop_evt = story_liker_stop_event

    setup_logging()
    _ensure_dashboard_log_handler()

    pass_num = 0

    while not stop_evt.is_set():
        pass_num += 1
        state_obj["status"] = "running"
        state_obj["last_run_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"♻️ CONTINUOUS STORY LIKER PASS #{pass_num} STARTING")

        from bot import run_story_liker_bot
        try:
            run_story_liker_bot(
                stop_event=stop_evt,
                account_owner=account_owner,
                continuous_mode=True,
                state_obj=state_obj,
            )
        except Exception as e:
            logger.error(f"Continuous Story Liker pass #{pass_num} crashed: {e}")

        state_obj["last_run_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _refresh_total_story_likes()

        if stop_evt.is_set():
            break
        if stop_evt.wait(1.0):
            break

    state_obj["status"] = "stopped"
    logger.info("🛑 Story Liker Bot runner stopped.")


# ── Flask Routes ──
@app.route("/login", methods=["GET", "POST"])
def login():
  if session.get("authenticated"):
    return redirect(url_for("dashboard"))

  error = None
  if request.method == "POST":
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    auth_user = database.authenticate_user(username, password)
    if auth_user:
      session.clear()
      session.permanent = True
      session["authenticated"] = True
      session["username"] = auth_user["username"]
      session["role"] = auth_user["role"]

      _log_actor_action(
        "login",
        target_type="auth",
        target_value="dashboard",
        details={"ip": request.remote_addr or ""},
        employees_only=True,
      )
      return redirect(url_for("dashboard"))

    error = "Invalid username or password"

  return render_template("login.html", error=error)


@app.route("/logout")
def logout():
  session.clear()
  return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    dm_log = database.get_dm_logs()
    dm_log_count = len(dm_log)
    user_ctx = _current_user_context()

    return render_template(
        "index.html",
        dm_state=dm_bot_state,
        dm_log_count=dm_log_count,
        current_user=user_ctx,
    )

# ── API ──
@app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    """Retrieve all configuration chunks to populate the UI."""
    user_ctx = _current_user_context()
    data = {
        "accounts": [],
        "accounts_queue": [],
        "models": [],
        "messages": [],
        "settings": {},
        "users": [],
        "current_user": user_ctx,
    }
    try:
        if user_ctx["role"] == "master":
            data["accounts"] = database.get_accounts(include_all=True)
            data["users"] = database.get_users()
            queue_rows = database.get_accounts(include_all=True)
            data["accounts_queue"] = [
                {
                    "username": str(acc.get("username", "")).strip(),
                    "owner_username": str(acc.get("owner_username", "")).strip() or "master",
                    "model_label": str(acc.get("model_label", "")).strip(),
                    "proxy": _mask_proxy_for_view(acc.get("proxy", "")),
              "profile_note": str(acc.get("profile_note", "")).strip(),
              "automation_enabled": _normalize_bool_flag(acc.get("automation_enabled", True), default=True),
              "is_suspended": _normalize_bool_flag(acc.get("is_suspended", False), default=False),
                }
                for acc in queue_rows
                if str(acc.get("username", "")).strip()
            ]
        else:
            data["accounts"] = database.get_accounts(owner_username=user_ctx["username"])
            queue_rows = database.get_accounts(include_all=True)
            data["accounts_queue"] = [
                {
                    "username": str(acc.get("username", "")).strip(),
                    "owner_username": str(acc.get("owner_username", "")).strip() or "master",
                    "model_label": str(acc.get("model_label", "")).strip(),
                    "proxy": _mask_proxy_for_view(acc.get("proxy", "")),
              "profile_note": str(acc.get("profile_note", "")).strip(),
              "automation_enabled": _normalize_bool_flag(acc.get("automation_enabled", True), default=True),
              "is_suspended": _normalize_bool_flag(acc.get("is_suspended", False), default=False),
                }
                for acc in queue_rows
                if str(acc.get("username", "")).strip()
            ]

        data["models"] = database.get_models()
        data["messages"] = database.get_messages()
        data["settings"] = database.get_all_settings()
    except Exception as e:
        logger.error(f"Error reading config API: {e}")
    return jsonify(data)


@app.route("/api/accounts/queue", methods=["GET"])
@login_required
@master_required
def api_accounts_queue():
    """Return sanitized cross-employee IG account queue."""
    try:
        queue_rows = database.get_accounts(include_all=True)
        accounts = [
            {
                "username": str(acc.get("username", "")).strip(),
                "owner_username": str(acc.get("owner_username", "")).strip() or "master",
            "model_label": str(acc.get("model_label", "")).strip(),
            "proxy": _mask_proxy_for_view(acc.get("proxy", "")),
            "profile_note": str(acc.get("profile_note", "")).strip(),
          "automation_enabled": _normalize_bool_flag(acc.get("automation_enabled", True), default=True),
          "is_suspended": _normalize_bool_flag(acc.get("is_suspended", False), default=False),
            }
            for acc in queue_rows
            if str(acc.get("username", "")).strip()
        ]
        return jsonify({"success": True, "accounts": accounts})
    except Exception as e:
        logger.error(f"Error reading accounts queue: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/accounts/proxy", methods=["POST"])
@login_required
def api_update_account_proxy():
  """Allow proxy updates only for accessible accounts (owner or master)."""
  try:
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates", [])
    if not isinstance(updates, list):
      return jsonify({"success": False, "error": "updates must be a list"}), 400

    user_ctx = _current_user_context()
    changed_usernames = []
    for idx, item in enumerate(updates):
      if not isinstance(item, dict):
        return jsonify({"success": False, "error": f"Invalid update entry at index {idx}"}), 400

      username = str(item.get("username", "")).strip()
      if not username:
        continue

      if not database.user_can_access_account(username, user_ctx["username"], user_ctx["role"]):
        return jsonify({"success": False, "error": f"Forbidden for account '{username}'"}), 403

      proxy_value, too_many_proxies, _ = _normalize_proxy_value(item.get("proxy", ""))
      if too_many_proxies:
        return jsonify({
          "success": False,
          "error": f"Account '{username}' supports maximum {MAX_PROXIES_PER_ACCOUNT} proxies",
        }), 400

      updated = database.update_account_proxy(username, proxy_value)
      if updated:
        changed_usernames.append(username)

    if changed_usernames:
      _log_actor_action(
        "update_account_proxy",
        target_type="config",
        target_value="accounts.proxy",
        details={
          "updated_count": len(changed_usernames),
          "usernames": ", ".join(changed_usernames[:20]) + (" ..." if len(changed_usernames) > 20 else ""),
          "actor_role": user_ctx.get("role", "employee"),
        },
        employees_only=True,
      )

    return jsonify({"success": True, "updated_count": len(changed_usernames)})
  except ValueError as e:
    return jsonify({"success": False, "error": str(e)}), 400
  except Exception as e:
    logger.error(f"Error updating account proxy values: {e}")
    return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/config/<target>", methods=["POST"])
@login_required
def api_save_config(target):
    """Save configuration changes back to database."""
    try:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {} if target in ("settings", "model_message_map") else []
        user_ctx = _current_user_context()
        is_master = user_ctx["role"] == "master"
        can_edit_targets = user_ctx["role"] in ("master", "employee")

        if target == "settings":
            if not is_master:
                return jsonify({"success": False, "error": "Only master can update settings"}), 403
            database.save_settings(payload)
        elif target == "accounts":
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Accounts payload must be a list"}), 400

            clean_accounts = []
            for idx, raw_acc in enumerate(payload):
                if not isinstance(raw_acc, dict):
                    return jsonify({"success": False, "error": f"Invalid account entry at index {idx}"}), 400

                username = str(raw_acc.get("username", "")).strip()
                if not username:
                    continue

                password = str(raw_acc.get("password", "")).strip()
                if not password:
                    return jsonify({
                        "success": False,
                        "error": f"Password is required for account '{username}'",
                    }), 400

                profile_note = str(raw_acc.get("profile_note", "") or "").strip()
                if not profile_note:
                  return jsonify({
                    "success": False,
                    "error": f"Bio + URL is required for account '{username}'",
                  }), 400

                proxy_value, too_many_proxies, _ = _normalize_proxy_value(raw_acc.get("proxy", ""))
                if too_many_proxies:
                  return jsonify({
                    "success": False,
                    "error": f"Account '{username}' supports maximum {MAX_PROXIES_PER_ACCOUNT} proxies",
                  }), 400

                account_entry = {
                    "username": username,
                    "password": password,
                    "model_label": str(raw_acc.get("model_label", "")).strip(),
                    "custom_messages": _normalize_text_list(raw_acc.get("custom_messages", [])),
                    "proxy": proxy_value,
                    "profile_note": profile_note,
                  "automation_enabled": _normalize_bool_flag(raw_acc.get("automation_enabled", True), default=True),
                  "is_suspended": _normalize_bool_flag(raw_acc.get("is_suspended", False), default=False),
                }
                if account_entry["is_suspended"]:
                    account_entry["automation_enabled"] = False
                if is_master:
                    account_entry["owner_username"] = str(raw_acc.get("owner_username", "")).strip().lower() or "master"

                clean_accounts.append(account_entry)

            if is_master:
                database.save_accounts(clean_accounts, include_all=True)
            else:
                database.save_accounts(clean_accounts, owner_username=user_ctx["username"], include_all=False)
            _log_actor_action(
              "update_accounts",
              target_type="config",
              target_value="accounts",
              details={
                "account_count": len(clean_accounts),
              },
              employees_only=True,
            )
        elif target == "models":
            if not can_edit_targets:
                return jsonify({"success": False, "error": "Not allowed to update models"}), 403
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Models payload must be a list"}), 400
            clean_models = []
            seen_models = set()
            for model in payload:
                name = str(model or "").strip().lstrip("@")
                key = name.lower()
                if not name or key in seen_models:
                    continue
                seen_models.add(key)
                clean_models.append(name)
            database.save_models(clean_models)
            _log_actor_action(
                "update_models",
                target_type="config",
                target_value="models",
                details={
                    "model_count": len(clean_models),
                    "model_names": ", ".join(clean_models[:10]) + (" ..." if len(clean_models) > 10 else ""),
                },
                employees_only=False,
            )
        elif target == "messages":
            if not can_edit_targets:
                return jsonify({"success": False, "error": "Not allowed to update messages"}), 403
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Messages payload must be a list"}), 400
            clean_messages = [str(msg or "").strip() for msg in payload if str(msg or "").strip()]
            sample_messages = "; ".join(clean_messages[:3])
            if len(sample_messages) > 120:
                sample_messages = sample_messages[:117] + "..."
            database.save_messages(clean_messages)
            _log_actor_action(
                "update_messages",
                target_type="config",
                target_value="messages",
                details={
                    "message_count": len(clean_messages),
                    "message_sample": sample_messages,
                },
                employees_only=False,
            )

        elif target == "model_message_map":
            if not can_edit_targets:
                return jsonify({"success": False, "error": "Not allowed to update model-specific messages"}), 403
            if not isinstance(payload, dict):
                return jsonify({"success": False, "error": "MODEL_MESSAGE_MAP payload must be an object"}), 400

            # Backward-compatible payload support:
            # 1) legacy: {"model": ["msg1", ...]}
            # 2) current: {"model_message_map": {...}, "model_automation_map": {...}}
            if "model_message_map" in payload or "model_automation_map" in payload:
                raw_payload_model_map = payload.get("model_message_map", {})
                raw_payload_automation_map = payload.get("model_automation_map", {})
            else:
                raw_payload_model_map = payload
                raw_payload_automation_map = {}

            if not isinstance(raw_payload_model_map, dict):
                return jsonify({"success": False, "error": "model_message_map must be an object"}), 400
            if not isinstance(raw_payload_automation_map, dict):
                return jsonify({"success": False, "error": "model_automation_map must be an object"}), 400

            actor_username = str(user_ctx.get("username") or "unknown")
            now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

            existing_settings = database.get_all_settings()
            raw_existing_map = existing_settings.get("MODEL_MESSAGE_MAP", {})
            existing_map = raw_existing_map if isinstance(raw_existing_map, dict) else {}
            raw_existing_meta = existing_settings.get("MODEL_MESSAGE_META", {})
            existing_meta = raw_existing_meta if isinstance(raw_existing_meta, dict) else {}

            raw_existing_automation_map = existing_settings.get("MODEL_AUTOMATION_MAP", {})
            existing_automation_map = {}
            if isinstance(raw_existing_automation_map, dict):
                for raw_model, raw_enabled in raw_existing_automation_map.items():
                    model_key = str(raw_model or "").strip().lstrip("@").lower()
                    if not model_key:
                        continue
                    existing_automation_map[model_key] = _normalize_bool_flag(raw_enabled, default=True)

            payload_automation_map = {}
            for raw_model, raw_enabled in raw_payload_automation_map.items():
                model_key = str(raw_model or "").strip().lstrip("@").lower()
                if not model_key:
                    continue
                payload_automation_map[model_key] = _normalize_bool_flag(raw_enabled, default=True)

            normalized_map = {}
            for raw_model, raw_messages in raw_payload_model_map.items():
                model_key = str(raw_model or "").strip().lstrip("@").lower()
                if not model_key or not isinstance(raw_messages, list):
                    continue

                clean_messages = [str(msg or "").strip() for msg in raw_messages if str(msg or "").strip()]
                if not clean_messages:
                    continue

                normalized_map[model_key] = clean_messages

            normalized_automation_map = {}
            for model_key in normalized_map.keys():
                if model_key in payload_automation_map:
                    normalized_automation_map[model_key] = payload_automation_map[model_key]
                elif model_key in existing_automation_map:
                    normalized_automation_map[model_key] = existing_automation_map[model_key]
                else:
                    normalized_automation_map[model_key] = True

            model_meta = {}
            total_messages = 0
            for model_key, clean_messages in normalized_map.items():
                total_messages += len(clean_messages)

                prev_meta = existing_meta.get(model_key, {})
                if not isinstance(prev_meta, dict):
                    prev_meta = {}

                prev_messages_raw = existing_map.get(model_key, [])
                prev_messages = (
                    [str(msg or "").strip() for msg in prev_messages_raw if str(msg or "").strip()]
                    if isinstance(prev_messages_raw, list)
                    else []
                )
                is_changed = prev_messages != clean_messages

                created_by = str(prev_meta.get("created_by") or actor_username)
                created_at = str(prev_meta.get("created_at") or now_iso)

                if is_changed:
                    updated_by = actor_username
                    updated_at = now_iso
                else:
                    updated_by = str(prev_meta.get("updated_by") or created_by)
                    updated_at = str(prev_meta.get("updated_at") or created_at)

                model_meta[model_key] = {
                    "created_by": created_by,
                    "created_at": created_at,
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                }

            model_names = list(normalized_map.keys())
            database.save_settings({
                "MODEL_MESSAGE_MAP": normalized_map,
                "MODEL_MESSAGE_META": model_meta,
                "MODEL_AUTOMATION_MAP": normalized_automation_map,
            })
            _log_actor_action(
                "update_model_message_map",
                target_type="config",
                target_value="MODEL_MESSAGE_MAP",
                details={
                    "model_entry_count": len(model_names),
                    "message_count": total_messages,
                    "disabled_model_count": sum(1 for v in normalized_automation_map.values() if not bool(v)),
                    "model_names": ", ".join(model_names[:10]) + (" ..." if len(model_names) > 10 else ""),
                },
                employees_only=False,
            )
            return jsonify({
                "success": True,
                "model_message_map": normalized_map,
                "model_message_meta": model_meta,
                "model_automation_map": normalized_automation_map,
            })
        else:
            return jsonify({"success": False, "error": "Invalid target"}), 400

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving {target}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users", methods=["POST"])
@login_required
@master_required
def api_create_user():
    """Create a new dashboard user (master-only)."""
    try:
        payload = request.get_json() or {}
        user = database.create_user(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("role", "employee"),
        )
        return jsonify({"success": True, "user": user})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>/password", methods=["POST"])
@login_required
@master_required
def api_update_user_password(username):
    """Reset password for a dashboard user (master-only)."""
    try:
        payload = request.get_json() or {}
        ok = database.update_user_password(username, payload.get("password", ""))
        if not ok:
            return jsonify({"success": False, "error": "User not found"}), 404
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error updating user password: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>", methods=["PUT", "POST"])
@app.route("/api/users/<username>/update", methods=["POST"])
@login_required
@master_required
def api_update_user_credentials(username):
    """Update dashboard username and/or password (master-only)."""
    try:
        payload = request.get_json() or {}
        result = database.update_user_credentials(
            username,
            new_username=payload.get("username"),
            new_password=payload.get("password"),
        )
        if not result:
            return jsonify({"success": False, "error": "User not found"}), 404

        # Keep current session consistent if the active master renamed this account.
        if session.get("username", "").strip().lower() == result["old_username"]:
            session["username"] = result["username"]

        return jsonify({"success": True, "user": result})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error updating user credentials: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>", methods=["DELETE"])
@login_required
@master_required
def api_delete_user(username):
    """Delete a dashboard user (master-only)."""
    try:
        deleted = database.delete_user(username)
        if not deleted:
            return jsonify({"success": False, "error": "User not found"}), 404
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error deleting user: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cookies/<username>", methods=["GET"])
@login_required
def api_get_cookies(username):
    """Retrieve raw cookies JSON for an account if it exists."""
    try:
        user_ctx = _current_user_context()
        if not database.user_can_access_account(username, user_ctx["username"], user_ctx["role"]):
            return jsonify({"success": False, "error": "Forbidden"}), 403

        cookies_list = database.get_cookies(username)
        if cookies_list:
            return jsonify({"success": True, "cookies": json.dumps(cookies_list, indent=2)})
        return jsonify({"success": True, "cookies": ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cookies/<username>", methods=["POST"])
@login_required
def api_save_cookies(username):
    """Save raw cookies JSON directly to the database."""
    try:
      user_ctx = _current_user_context()
      if not database.user_can_access_account(username, user_ctx["username"], user_ctx["role"]):
        return jsonify({"success": False, "error": "Forbidden"}), 403

      payload = request.get_json()
      cookies_str = payload.get("cookies", "")
      cookie_count = 0

      if not cookies_str.strip():
        database.save_cookies(username, [])
      else:
        cookies_list = json.loads(cookies_str)
        database.save_cookies(username, cookies_list)
        cookie_count = len(cookies_list) if isinstance(cookies_list, list) else 1

      _log_actor_action(
        "save_cookies",
        target_type="account",
        target_value=username,
        details={
          "cookie_count": cookie_count,
          "cleared": cookie_count == 0,
        },
        employees_only=True,
      )

      return jsonify({"success": True})
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Invalid JSON format"}), 400
    except Exception as e:
        logger.error(f"Error saving cookies for {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/activity/employee", methods=["GET"])
@login_required
@master_required
def api_employee_activity():
    """Get recent employee actions for the master dashboard."""
    try:
        limit_raw = request.args.get("limit", "150")
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 150

        logs = database.get_activity_logs(limit=limit, employees_only=True)
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        logger.error(f"Error loading employee activity: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/activity/all", methods=["GET"])
@login_required
@master_required
def api_all_activity():
    """Get all dashboard activity for the last N hours (default: 24)."""
    try:
        limit_raw = request.args.get("limit", "500")
        hours_raw = request.args.get("hours", "24")

        try:
            limit = int(limit_raw)
        except Exception:
            limit = 500

        try:
            hours = int(hours_raw)
        except Exception:
            hours = 24

        logs = database.get_activity_logs_recent_hours(hours=hours, limit=limit, employees_only=False)

        day_counts = {}
        for row in logs:
            day_key = str(row.get("created_at") or "").strip()[:10] or "-"
            day_counts[day_key] = day_counts.get(day_key, 0) + 1

        by_day = [
            {"day": day, "count": day_counts[day]}
            for day in sorted(day_counts.keys(), reverse=True)
        ]

        return jsonify({
            "success": True,
            "logs": logs,
            "by_day": by_day,
            "hours": hours,
        })
    except Exception as e:
        logger.error(f"Error loading all activity: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
@login_required
@master_required
def api_status():
    _refresh_total_dms_all_time()
    _refresh_total_likes()
    _refresh_total_story_likes()
    _ensure_telegram_polling()
    response = {
        "dm": dm_bot_state,
        "liker": liker_bot_state,
        "story": story_liker_bot_state,
    }
    cluster_control = _get_cluster_control_payload()
    if cluster_control:
        response["cluster_desired_state"] = cluster_control.get("desired_state", "")
        response["cluster_issued_by"] = cluster_control.get("issued_by", "")
        response["cluster_issued_at"] = cluster_control.get("issued_at", "")
    return jsonify(response)


@app.after_request
def add_no_cache_headers(response):
    """Prevent stale dashboard/template content in browser cache."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/start")
@login_required
def start_bot():
    user_ctx = _current_user_context()
    # if _is_bot_running():
    #     logger.info("Start requested but bot loop is already running.")
    #     return "<script>window.location='/'</script>"



    try:
      _publish_cluster_control(
        desired_state="running",
        issued_by=user_ctx["username"],
        issued_by_role=user_ctx["role"],
        run_mode="dm",
      )
    except Exception as e:
      logger.warning(f"Failed to publish cluster start command: {e}")

    started = _start_bot_loop(
      started_by=user_ctx["username"],
      started_by_role=user_ctx["role"]
    )
    if not started:
        logger.info("Start requested but bot loop is already running.")
        return "<script>window.location='/'</script>"

    _log_actor_action(
        "start_bot",
        target_type="runtime",
        target_value="bot",
      details={"status": "running", "scope": "cluster"},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"


@app.route("/start/liker")
@login_required
def start_liker_bot():
    user_ctx = _current_user_context()
    global liker_bot_thread

    try:
      _publish_cluster_control(
        desired_state="running",
        issued_by=user_ctx["username"],
        issued_by_role=user_ctx["role"],
        run_mode="liker",
      )
    except Exception as e:
      logger.warning(f"Failed to publish cluster start command (liker): {e}")

    with liker_bot_thread_lock:
        if liker_bot_thread and liker_bot_thread.is_alive():
            return "<script>window.location='/'</script>"

        liker_stop_event.clear()
        liker_bot_state["status"] = "running"
        
        # Only restrict to owner if they are an employee. Masters see everything.
        owner = user_ctx["username"] if user_ctx["role"] == "employee" else None
        
        liker_bot_thread = threading.Thread(target=liker_loop, args=(owner,), daemon=True)
        liker_bot_thread.start()

    _log_actor_action(
        "start_liker_bot",
        target_type="runtime",
        target_value="liker_bot",
        details={"status": "running"},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"


@app.route("/start/story")
@login_required
def start_story_liker_bot():
    user_ctx = _current_user_context()
    global story_liker_bot_thread

    try:
      _publish_cluster_control(
        desired_state="running",
        issued_by=user_ctx["username"],
        issued_by_role=user_ctx["role"],
        run_mode="story",
      )
    except Exception as e:
      logger.warning(f"Failed to publish cluster start command (story): {e}")

    with story_liker_bot_thread_lock:
        if story_liker_bot_thread and story_liker_bot_thread.is_alive():
            return "<script>window.location='/'</script>"

        story_liker_stop_event.clear()
        story_liker_bot_state["status"] = "running"

        owner = user_ctx["username"] if user_ctx["role"] == "employee" else None

        story_liker_bot_thread = threading.Thread(target=story_liker_loop, args=(owner,), daemon=True)
        story_liker_bot_thread.start()

    _log_actor_action(
        "start_story_liker_bot",
        target_type="runtime",
        target_value="story_liker_bot",
        details={"status": "running"},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"




@app.route("/stop")
@login_required
def stop_bot():
    user_ctx = _current_user_context()
    mode = request.args.get("mode", "all")

    run_mode = _normalize_run_mode(mode) or (
        "all" if str(mode).strip().lower() == "all" else "dm"
    )

    try:
        _publish_cluster_control(
            desired_state="stopped",
            issued_by=user_ctx["username"],
            issued_by_role=user_ctx["role"],
            run_mode=run_mode,
        )
    except Exception as e:
        logger.warning(f"Failed to publish cluster stop command: {e}")

    _request_local_stop(mode=run_mode)

    _log_actor_action(
        "stop_bot",
        target_type="runtime",
        target_value=f"bot_{mode}",
        details={"status": "stopped", "scope": "cluster", "mode": mode},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"


@app.route("/stop/liker")
@login_required
def stop_liker_bot():
    user_ctx = _current_user_context()
    try:
      _publish_cluster_control(
        desired_state="stopped",
        issued_by=user_ctx["username"],
        issued_by_role=user_ctx["role"],
        run_mode="liker",
      )
    except Exception as e:
      logger.warning(f"Failed to publish cluster stop command (liker): {e}")

    liker_stop_event.set()
    liker_bot_state["status"] = "stopping"
    
    _log_actor_action(
        "stop_liker_bot",
        target_type="runtime",
        target_value="liker_bot",
        details={"status": "stopping"},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"


@app.route("/stop/story")
@login_required
def stop_story_liker_bot():
    user_ctx = _current_user_context()
    try:
      _publish_cluster_control(
        desired_state="stopped",
        issued_by=user_ctx["username"],
        issued_by_role=user_ctx["role"],
        run_mode="story",
      )
    except Exception as e:
      logger.warning(f"Failed to publish cluster stop command (story): {e}")

    story_liker_stop_event.set()
    story_liker_bot_state["status"] = "stopping"

    _log_actor_action(
        "stop_story_liker_bot",
        target_type="runtime",
        target_value="story_liker_bot",
        details={"status": "stopping"},
        employees_only=True,
    )
    return "<script>window.location='/'</script>"


# Main
if __name__ == "__main__":
    database.init_db()

    print("=" * 60)
    print("  INSTAGRAM DM BOT - FLASK SERVER")
    print("=" * 60)
    print("  Dashboard: http://localhost:5000")
    print(f"  System Booting...")
    print("=" * 60)
    print()

    # Bot will remain idle until started via the web UI dashboard.
    bot_state["status"] = "idle"

    _ensure_cluster_control_watcher()

    if _env_is_true("BOT_AUTO_START", default=False):
        started = _start_bot_loop(started_by="system", started_by_role="master")
        if started:
            logger.info("BOT_AUTO_START enabled. Bot loop started automatically.")
        else:
            logger.info("BOT_AUTO_START enabled but bot loop is already running.")

    app.run(host="0.0.0.0", port=5000, debug=False)
