"""
Main orchestrator — the brain of the Model DM Bot.
Coordinates accounts, models, scraping, DMs, and Telegram alerts.
"""
import json
import os
import sys
import time
import random
import re
import logging
import threading
from datetime import datetime, timedelta
from uuid import uuid4
from config.settings import LOGS_DIR
from config import database
from config.database import get_required_setting
from core.browser import create_driver, close_driver, _mask_proxy_for_log
from core.cookie_manager import save_cookies, refresh_cookies
from core.auth import (
    login_with_cookies, login_with_credentials,
    detect_challenge, handle_two_factor,
    is_logged_in, human_delay, ChallengeType,
    type_like_human, human_scroll,
)
from core.scraper import get_recent_posts, get_post_interactors, sort_posts_by_priority
from core.followers import get_followers
from core.dm_sender import send_dm, DMResult, wait_between_dms
from core.liker import run_comment_liker_script
from core.story_liker import run_story_liker_script
from core.distributed_coordination import DistributedCoordinator
from telegram.bot import telegram_bot

logger = logging.getLogger("model_dm_bot")
_active_drivers = set()
_active_drivers_lock = threading.Lock()
DM_SUMMARY_WINDOW_HOURS = 24
MAX_ACCOUNT_PROXIES = 5
CLUSTER_NOTIFICATION_COOLDOWN_SEC = 24 * 60 * 60
CLUSTER_NOTIFICATION_FALLBACK_BUCKET_SEC = 10 * 60
STORY_LIKE_MILESTONE_STEP = 100
STORY_LIKE_MILESTONE_COOLDOWN_SEC = 365 * 24 * 60 * 60

DM_BATCH_PAUSE_ENABLED_SETTING_KEY = "DM_BATCH_PAUSE_ENABLED"
DM_BATCH_SIZE = 10
DM_BATCH_COOLDOWN_SECONDS = 5 * 60


def _setting_int(key: str) -> int:
    """Read an integer setting from the database."""
    value = get_required_setting(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer setting '{key}': {value}")


def _setting_float(key: str) -> float:
    """Read a float setting from the database."""
    value = get_required_setting(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric setting '{key}': {value}")


def _setting_bool(key: str, default: bool = False) -> bool:
    """Read a boolean setting from the database."""
    value = database.get_setting(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return int(value) != 0

    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes", "enable", "enabled"):
        return True
    if text in ("0", "false", "off", "no", "disable", "disabled", "", "none", "null"):
        return False
    return bool(default)

def _setting_int_default(key: str, default: int) -> int:
    """Read an integer setting with a hard fallback when parsing fails."""
    value = database.get_setting(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _setting_text_list(key: str, default_values: list) -> list:
    """Read a text list setting from either list JSON or comma/newline text."""
    value = database.get_setting(key, default_values)

    items = []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"[\r\n,;]+", value)
    else:
        items = list(default_values or [])

    clean = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue

        key_text = text.lower()
        if key_text in seen:
            continue

        seen.add(key_text)
        clean.append(text)

    if clean:
        return clean

    return [str(item or "").strip() for item in (default_values or []) if str(item or "").strip()]


def _interruptible_sleep(seconds: float, stop_event=None, tick: float = 0.5) -> bool:
    """Sleep in short ticks so stop requests can interrupt long waits."""
    end_time = time.time() + max(0, seconds)
    while time.time() < end_time:
        if stop_event and stop_event.is_set():
            return True
        remaining = end_time - time.time()
        time.sleep(min(tick, max(0.0, remaining)))
    return False






def _maybe_wait_for_dm_batch_cooldown(sender: str, dm_batch_state: dict, stop_event=None):
    """Pause after each full DM batch for one account when safety toggle is enabled."""
    if not isinstance(dm_batch_state, dict):
        return

    if not _setting_bool(DM_BATCH_PAUSE_ENABLED_SETTING_KEY, default=False):
        return

    sent_count = int(dm_batch_state.get("sent_count", 0) or 0)
    if sent_count <= 0 or (sent_count % DM_BATCH_SIZE) != 0:
        return

    last_cooldown_count = int(dm_batch_state.get("last_cooldown_count", 0) or 0)
    if last_cooldown_count == sent_count:
        return

    cooldown_minutes = int(DM_BATCH_COOLDOWN_SECONDS // 60)
    log_and_telegram(
        f"[{sender}] 🛡️ Safety pause: {DM_BATCH_SIZE} DMs sent. Waiting {cooldown_minutes} minutes before next batch..."
    )
    interrupted = _interruptible_sleep(DM_BATCH_COOLDOWN_SECONDS, stop_event=stop_event)
    dm_batch_state["last_cooldown_count"] = sent_count

    if interrupted:
        log_and_telegram(f"[{sender}] 🛑 Safety pause interrupted by stop request.")
    else:
        log_and_telegram(f"[{sender}] ▶️ Safety pause complete. Continuing DM batch.")


def _register_driver(driver):
    with _active_drivers_lock:
        _active_drivers.add(driver)


def _unregister_driver(driver):
    with _active_drivers_lock:
        _active_drivers.discard(driver)


def force_stop_active_sessions():
    """Force-close active browsers so stop requests interrupt current Selenium tasks."""
    with _active_drivers_lock:
        drivers = list(_active_drivers)
        _active_drivers.clear()

    for drv in drivers:
        try:
            close_driver(drv)
        except Exception:
            pass


def _safe_remove_file(file_path: str):
    target = str(file_path or "").strip()
    if not target:
        return

    try:
        if os.path.isfile(target):
            os.remove(target)
    except Exception:
        pass


def setup_logging():
    """Configure logging to file and console (only once)."""
    root_logger = logging.getLogger("model_dm_bot")
    if root_logger.handlers:
        return  # Already set up

    log_file = os.path.join(LOGS_DIR, "bot.log")
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(logging.INFO)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.INFO)

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(fh)
    root_logger.addHandler(ch)


# Old JSON functions removed. Bot now relies on Database.


def log_and_telegram(msg: str):
    """Log a message and add it to Telegram's log buffer."""
    logger.info(msg)
    telegram_bot.add_log(msg)


def _maybe_send_story_like_milestone(account: str, model: str):
    """Send a Telegram notification after each milestone of story likes across the cluster."""
    try:
        metrics = database.get_story_liker_dashboard_metrics()
        lifetime_total = int(metrics.get("lifetime_story_likes", 0) or 0)
    except Exception:
        return

    if lifetime_total <= 0 or lifetime_total % STORY_LIKE_MILESTONE_STEP != 0:
        return

    event_key = f"story_like_milestone:{lifetime_total}"
    if not database.claim_notification_event(
        event_key,
        cooldown_seconds=STORY_LIKE_MILESTONE_COOLDOWN_SEC,
    ):
        return

    telegram_bot.send_story_like_milestone(lifetime_total, account, model)


def _check_for_challenges_and_alert(driver, username, context="during interaction") -> bool:
    """Check for challenges and send Telegram alerts if found."""
    challenge = detect_challenge(driver)
    if challenge == ChallengeType.NONE:
        return False
    
    log_and_telegram(f"[{username}] ⚠️ Challenge detected {context}: {challenge.value}")
    telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
    
    if challenge == ChallengeType.LOCKED:
        telegram_bot.send_lockout_alert(username, f"Account locked {context}")
        _mark_account_suspended(username, f"locked {context}")
        
    return True


def _is_page_unavailable(driver) -> bool:
    """Detect 'Sorry, this page isn't available' Instagram error."""
    try:
        # 1. Check title (Instagram usually sets title to 'Page not found • Instagram' or just 'Instagram')
        title = str(driver.title or "").lower()
        if "page not found" in title or title == "instagram":
            return True

        # 2. Check for the specific error text in the page source
        # This covers the exact HTML snippet provided by the user
        source = driver.page_source.lower()
        error_markers = [
            "sorry, this page isn't available",
            "the link you followed may be broken",
            "page may have been removed",
        ]
        
        for marker in error_markers:
            if marker in source:
                return True
                
        # 3. Check for specific CSS classes or layout if text check is too broad
        # (Optional: can add specific selector checks here if needed)
        
    except Exception:
        pass
    return False


def _mark_account_suspended(username: str, reason: str = ""):
    """Persist account as suspended so future sessions skip it automatically."""
    clean_username = str(username or "").strip().lstrip("@")
    if not clean_username:
        return

    try:
        already_suspended = database.is_account_suspended(clean_username, default=False)
    except Exception:
        already_suspended = False

    try:
        database.set_account_suspended(clean_username, True)
    except Exception as e:
        logger.warning(f"Failed to mark @{clean_username} as suspended: {e}")
        return

    if not already_suspended:
        note = f" ({reason})" if str(reason or "").strip() else ""
        log_and_telegram(f"⛔ @{clean_username} moved to Suspended Accounts{note}.")


def _is_expected_driver_shutdown_error(err: Exception) -> bool:
    """Return True for common Selenium transport errors triggered by forced stop."""
    text = str(err or "").strip().lower()
    if not text:
        return False

    markers = (
        "httpconnectionpool(host='localhost'",
        "max retries exceeded with url: /session/",
        "newconnectionerror",
        "failed to establish a new connection",
        "winerror 10061",
        "connection refused",
        "invalid session id",
        "no such window",
        "target window already closed",
        "chrome not reachable",
        "not connected to devtools",
        "disconnected:",
        "connection aborted",
        "remote end closed connection",
    )
    return any(marker in text for marker in markers)


def _parse_iso_datetime(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except Exception:
        return None

    if dt.tzinfo is not None:
        try:
            return dt.astimezone().replace(tzinfo=None)
        except Exception:
            return dt.replace(tzinfo=None)
    return dt


def _is_dm_summary_due(hours: int = DM_SUMMARY_WINDOW_HOURS) -> bool:
    safe_hours = max(1, int(hours or DM_SUMMARY_WINDOW_HOURS))
    last_sent_raw = database.get_setting("DM_24H_REPORT_LAST_SENT_AT", "")
    last_sent = _parse_iso_datetime(last_sent_raw)

    # First run initializes the timer; the first summary will be sent after the window elapses.
    if last_sent is None:
        database.save_settings({"DM_24H_REPORT_LAST_SENT_AT": datetime.now().isoformat(timespec="seconds")})
        return False

    return (datetime.now() - last_sent) >= timedelta(hours=safe_hours)


def _maybe_send_24h_dm_summary(hours: int = DM_SUMMARY_WINDOW_HOURS, force: bool = False) -> bool:
    safe_hours = max(1, int(hours or DM_SUMMARY_WINDOW_HOURS))

    try:
        if not force and not _is_dm_summary_due(safe_hours):
            return False

        summary = database.get_dm_sent_summary_last_hours(
            hours=safe_hours,
            include_all_accounts=True,
        )
        telegram_bot.send_24h_dm_summary(summary)
        database.save_settings({"DM_24H_REPORT_LAST_SENT_AT": datetime.now().isoformat(timespec="seconds")})

        logger.info(
            "24h DM summary sent to Telegram (window=%sh, total_sent=%s, lifetime_total_sent=%s)",
            safe_hours,
            summary.get("total_sent", 0),
            summary.get("lifetime_total_sent", 0),
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to send 24h DM summary: {e}")
        return False


def _normalize_model_key(model_username: str) -> str:
    """Normalize model usernames to a stable lookup key."""
    return str(model_username or "").strip().lstrip("@").lower()


def _normalize_account_model_label(raw_label: str) -> str:
    """Normalize an account model label; empty means generic account."""
    key = _normalize_model_key(raw_label)
    if key in ("", "generic", "any", "all", "*", "none"):
        return ""
    return key


def _cluster_control_snapshot() -> dict:
    raw = database.get_setting("BOT_CLUSTER_CONTROL", {})
    if not isinstance(raw, dict):
        return {}

    desired_state = str(raw.get("desired_state", "") or "").strip().lower()
    nonce = str(raw.get("nonce", "") or "").strip().lower()

    return {
        "desired_state": desired_state,
        "nonce": nonce,
    }


def _claim_cluster_notification(event_name: str, expected_state: str = "") -> bool:
    """Claim one cluster-wide notification slot using DB-backed dedupe."""
    clean_event_name = str(event_name or "").strip().lower()
    if not clean_event_name:
        return False

    control = _cluster_control_snapshot()
    desired_state = str(control.get("desired_state", "") or "").strip().lower()
    nonce = str(control.get("nonce", "") or "").strip().lower()

    if expected_state and desired_state != str(expected_state).strip().lower():
        return False

    if nonce:
        dedupe_key = f"telegram:{clean_event_name}:{nonce}"
        return database.claim_notification_event(dedupe_key, cooldown_seconds=CLUSTER_NOTIFICATION_COOLDOWN_SEC)

    # Fallback when cluster state has no nonce (e.g. legacy/manual start).
    time_bucket = int(time.time() // CLUSTER_NOTIFICATION_FALLBACK_BUCKET_SEC)
    dedupe_key = f"telegram:{clean_event_name}:fallback:{time_bucket}"
    return database.claim_notification_event(
        dedupe_key,
        cooldown_seconds=CLUSTER_NOTIFICATION_FALLBACK_BUCKET_SEC,
    )


def _account_label_meta(account: dict):
    """Return normalized label key and display name for an account row."""
    raw_label = str((account or {}).get("model_label", "")).strip().lstrip("@")
    label_key = _normalize_account_model_label(raw_label)
    if label_key:
        return label_key, (raw_label or label_key)
    return "", "Generic"


def _sort_accounts_for_label_batches(accounts: list) -> list:
    """Randomize account order per run while keeping same-label accounts contiguous."""
    rows = list(accounts or [])
    if not rows:
        return []

    grouped = {}
    for account in rows:
        label_key, _ = _account_label_meta(account)
        group_key = label_key or "generic"
        grouped.setdefault(group_key, []).append(account)

    group_keys = list(grouped.keys())
    random.shuffle(group_keys)

    randomized = []
    for group_key in group_keys:
        group_rows = list(grouped.get(group_key, []))
        random.shuffle(group_rows)
        randomized.extend(group_rows)

    return randomized


def _count_accounts_by_label(accounts: list) -> dict:
    """Return {label_key_or_generic: {display, count}} for active accounts."""
    counts = {}
    for account in accounts or []:
        label_key, label_display = _account_label_meta(account)
        key = label_key or "generic"
        if key not in counts:
            counts[key] = {"display": label_display, "count": 0}
        counts[key]["count"] += 1
    return counts


def _models_for_account(account: dict, all_models: list) -> list:
    """Return target models for an account.

    Account labels are campaign/model-owner tags, not target usernames,
    so they should not restrict which targets this account can process.
    """
    models = list(all_models or [])
    random.shuffle(models)
    return models


def _pop_random_item(items: list):
    """Pop a random item from a list (returns None if empty)."""
    if not items:
        return None
    try:
        index = random.randrange(len(items))
    except ValueError:
        return None
    return items.pop(index)


def _build_account_pool_summary(accounts: list, models: list) -> str:
    """Build Telegram text for per-label and generic account availability."""
    display_by_key = {}
    for model_name in models:
        key = _normalize_model_key(model_name)
        if key:
            display_by_key[key] = str(model_name or "").strip().lstrip("@")

    counts_by_model = {}
    generic_count = 0

    for account in accounts:
        label_raw = str(account.get("model_label", "")).strip().lstrip("@")
        label_key = _normalize_account_model_label(label_raw)
        if not label_key:
            generic_count += 1
            continue

        counts_by_model[label_key] = counts_by_model.get(label_key, 0) + 1
        if label_key not in display_by_key:
            display_by_key[label_key] = label_raw or label_key

    ordered_keys = sorted(counts_by_model.keys(), key=lambda k: display_by_key.get(k, k).lower())
    label_width = len("Generic")
    for key in ordered_keys:
        label_width = max(label_width, len(str(display_by_key.get(key, key))))

    lines = []
    for key in ordered_keys:
        display_name = str(display_by_key.get(key, key)).strip() or key
        lines.append(f"• {display_name}: {counts_by_model[key]}")
    if generic_count > 0:
        lines.append(f"• Generic: {generic_count}")
    return "\n".join(lines)


def _normalize_message_list(raw_messages) -> list:
    """Normalize a raw messages array into non-empty trimmed strings."""
    if not isinstance(raw_messages, list):
        return []

    clean_messages = []
    for msg in raw_messages:
        if not isinstance(msg, str):
            continue
        trimmed = msg.strip()
        if trimmed:
            clean_messages.append(trimmed)
    return clean_messages


def _normalize_account_proxy_candidates(raw_proxy, max_items: int = MAX_ACCOUNT_PROXIES) -> list:
    """Parse account proxy input into an ordered unique list (up to max_items)."""
    raw_text = str(raw_proxy or "")
    if not raw_text.strip():
        return []

    candidates = []
    seen = set()
    for part in re.split(r"[\r\n,;]+", raw_text):
        proxy = str(part or "").strip()
        if not proxy:
            continue

        key = proxy.lower()
        if key in seen:
            continue

        seen.add(key)
        candidates.append(proxy)
        if len(candidates) >= max_items:
            break

    return candidates


def _normalize_model_message_map(raw_map) -> dict:
    """Normalize MODEL_MESSAGE_MAP from settings into {model_key: [messages]} format."""
    if not isinstance(raw_map, dict):
        return {}

    normalized = {}
    for raw_model, raw_messages in raw_map.items():
        model_key = _normalize_model_key(raw_model)
        if not model_key:
            continue

        messages = _normalize_message_list(raw_messages)
        if messages:
            normalized[model_key] = messages

    return normalized


def _normalize_model_automation_map(raw_map) -> dict:
    """Normalize MODEL_AUTOMATION_MAP from settings into {model_key: bool} format."""
    if not isinstance(raw_map, dict):
        return {}

    normalized = {}
    for raw_model, raw_enabled in raw_map.items():
        model_key = _normalize_model_key(raw_model)
        if not model_key:
            continue

        if isinstance(raw_enabled, bool):
            normalized[model_key] = raw_enabled
            continue

        if raw_enabled is None:
            normalized[model_key] = True
            continue

        if isinstance(raw_enabled, (int, float)):
            normalized[model_key] = int(raw_enabled) != 0
            continue

        text = str(raw_enabled).strip().lower()
        if text in ("0", "false", "off", "no", "disable", "disabled"):
            normalized[model_key] = False
        elif text in ("", "none", "null"):
            normalized[model_key] = True
        else:
            normalized[model_key] = True

    return normalized


def _messages_for_model(model_username: str, default_messages: list, model_message_map: dict) -> list:
    """Return custom messages for a model when available, otherwise global defaults."""
    custom_messages = model_message_map.get(_normalize_model_key(model_username), [])
    return custom_messages if custom_messages else default_messages


def run_bot(
    stop_event=None,
    account_owner=None,
    continuous_mode: bool = False,
):
    """Main bot orchestration loop."""
    database.init_db()
    setup_logging()
    coordinator = None
    distributed_session_id = uuid4().hex

    runtime_title = "INSTAGRAM MODEL DM BOT"

    logger.info("=" * 60)
    logger.info(f"  {runtime_title} — STARTING")
    logger.info("=" * 60)

    # Load config from Database
    try:
        settings_cache = database.get_all_settings()

        if account_owner:
            accounts = database.get_accounts(owner_username=account_owner)
        else:
            accounts = database.get_accounts(include_all=True)

        models = database.get_models()
        messages = _normalize_message_list(database.get_messages())
        model_message_map = _normalize_model_message_map(
            settings_cache.get("MODEL_MESSAGE_MAP") or {}
        )
        model_automation_map = _normalize_model_automation_map(
            settings_cache.get("MODEL_AUTOMATION_MAP") or {}
        )
        coordinator = DistributedCoordinator.from_settings(
            settings=settings_cache,
            logger=logger,
            account_owner=account_owner or "",
        )

        # If explicit model list is empty, derive targets from model-specific sets.
        if not models and model_message_map:
            models = sorted(model_message_map.keys())

        disabled_models = []
        enabled_models = []
        for model_name in models:
            model_key = _normalize_model_key(model_name)
            if model_key and not bool(model_automation_map.get(model_key, True)):
                disabled_models.append(str(model_name or "").strip().lstrip("@") or model_key)
                continue
            enabled_models.append(model_name)
        models = enabled_models
    except Exception as e:
        logger.error(f"Failed to load config from database: {e}")
        if coordinator:
            coordinator.shutdown()
        return

    suspended_accounts = [
        acc for acc in accounts
        if bool(acc.get("is_suspended", False))
    ]
    disabled_accounts = [
        acc for acc in accounts
        if not bool(acc.get("automation_enabled", True)) and not bool(acc.get("is_suspended", False))
    ]
    accounts = [
        acc for acc in accounts
        if bool(acc.get("automation_enabled", True)) and not bool(acc.get("is_suspended", False))
    ]
    accounts = _sort_accounts_for_label_batches(accounts)
    label_batch_counts = _count_accounts_by_label(accounts)

    if not accounts:
        if account_owner:
            logger.error(f"No automation-enabled accounts configured for employee @{account_owner}")
        else:
            logger.error("No automation-enabled accounts configured")
        if coordinator:
            coordinator.shutdown()
        return
    if not models:
        logger.error("No automation-enabled models configured in database")
        if coordinator:
            coordinator.shutdown()
        return
    if not messages and not model_message_map:
        logger.error("No messages configured (general or model-specific)")
        if coordinator:
            coordinator.shutdown()
        return

    logger.info(
        f"Loaded {len(accounts)} active accounts, {len(models)} active models, "
        f"{len(messages)} general messages, {len(model_message_map)} model-specific sets"
    )
    if disabled_accounts:
        logger.info(f"Automation disabled for {len(disabled_accounts)} account(s)")
    if suspended_accounts:
        logger.info(f"Suspended for safety: {len(suspended_accounts)} account(s)")
    if disabled_models:
        preview = ", ".join(f"@{str(model or '').strip().lstrip('@')}" for model in disabled_models[:20])
        suffix = " ..." if len(disabled_models) > 20 else ""
        logger.info(f"Automation disabled for {len(disabled_models)} model target(s): {preview}{suffix}")
    if account_owner:
        logger.info(f"Account scope: employee @{account_owner}")
    if label_batch_counts:
        ordered_label_items = sorted(
            label_batch_counts.items(),
            key=lambda item: (1 if item[0] == "generic" else 0, str(item[1].get("display", "")).lower()),
        )
        label_preview = ", ".join(
            f"{item[1].get('display', 'Generic')}({int(item[1].get('count', 0))})"
            for item in ordered_label_items
        )
        logger.info(f"Label batch order: {label_preview}")
    if coordinator and coordinator.enabled:
        if coordinator.is_active:
            logger.info(
                "Distributed coordination active (instance=%s, namespace=%s)",
                coordinator.instance_id,
                coordinator.namespace,
            )
        else:
            mode_label = "fail-closed" if coordinator.fail_closed else "best-effort"
            logger.warning(
                "Distributed coordination is enabled but Redis is unavailable (%s mode)",
                mode_label,
            )

    use_global_target_dedupe = _setting_bool("GLOBAL_TARGET_DEDUP_ENABLED", default=False)

    # Build per-session DM exclusion set. In global mode this includes the last
    # 24h of cluster DM history; otherwise it is local to this bot process.
    dm_log = {}
    already_dmd = set()
    if use_global_target_dedupe:
        dm_log = database.get_dm_logs()
        cutoff_time = datetime.now() - timedelta(hours=24)

        for user_dmd, timestamp_str in dm_log.items():
            try:
                if not timestamp_str:
                    already_dmd.add(user_dmd)
                    continue

                # support fromisoformat compatibility
                safe_ts = timestamp_str.replace("Z", "+00:00")
                dmd_time = datetime.fromisoformat(safe_ts)
                if dmd_time > cutoff_time:
                    already_dmd.add(user_dmd)
            except (ValueError, TypeError):
                # Fallback for old/corrupted formats
                already_dmd.add(user_dmd)

        logger.info("Global target dedupe enabled (24h cross-VPS target suppression)")
    else:
        logger.info("Global target dedupe disabled (each VPS/account chases its own DM quota)")

    logger.info("Runtime mode active: standard model DM flow")

    # Start Telegram
    telegram_bot.start_polling()
    should_send_startup_bundle = _claim_cluster_notification(
        event_name="bot_start",
        expected_state="running",
    )
    if should_send_startup_bundle:
        start_msg = "🚀 *DM BOT STARTED*"
        telegram_bot.send_startup(start_msg)
        telegram_bot.send_account_pool_summary(_build_account_pool_summary(accounts, models))
        telegram_bot.send_account_profile_summary(accounts, limit=3, recent_only=True)
    else:
        logger.info("Skipping duplicate cluster startup Telegram notifications on this VPS")
    if disabled_accounts:
        disabled_preview = ", ".join(
            f"@{str(acc.get('username', '')).strip().lstrip('@')}"
            for acc in disabled_accounts[:15]
            if str(acc.get("username", "")).strip()
        )
        suffix = " ..." if len(disabled_accounts) > 15 else ""
        log_and_telegram(
            f"👁️ Automation disabled for {len(disabled_accounts)} account(s): {disabled_preview}{suffix}"
        )
    if suspended_accounts:
        suspended_preview = ", ".join(
            f"@{str(acc.get('username', '')).strip().lstrip('@')}"
            for acc in suspended_accounts[:15]
            if str(acc.get("username", "")).strip()
        )
        suffix = " ..." if len(suspended_accounts) > 15 else ""
        log_and_telegram(
            f"⛔ Suspended accounts skipped for safety ({len(suspended_accounts)}): {suspended_preview}{suffix}"
        )
    _maybe_send_24h_dm_summary(hours=DM_SUMMARY_WINDOW_HOURS)
    telegram_bot.stats["status"] = "Running"
    telegram_bot.stats["current_account"] = "—"
    telegram_bot.stats["current_model"] = "—"

    total_dms_sent = 0
    completed_model_keys = set()
    session_account_dm_counts = {}
    active_label_key = None
    active_label_display = ""

    try:
        for account_index, account in enumerate(accounts):
            _maybe_send_24h_dm_summary(hours=DM_SUMMARY_WINDOW_HOURS)

            if stop_event and stop_event.is_set():
                scope_label = "pass" if continuous_mode else "session"
                log_and_telegram(f"🛑 Stop requested. Ending current {scope_label}.")
                break

            label_key, label_display = _account_label_meta(account)
            normalized_label_key = label_key or "generic"
            if normalized_label_key != active_label_key:
                if active_label_key is not None:
                    log_and_telegram(f"✅ Finished label batch: {active_label_display}")

                active_label_key = normalized_label_key
                active_label_display = label_display
                label_total = int((label_batch_counts.get(normalized_label_key) or {}).get("count", 0))
                log_and_telegram(
                    f"🏷️ Starting label batch: {active_label_display} ({label_total} account(s))"
                )

            username = account["username"]
            is_suspended_now = database.is_account_suspended(
                username,
                default=bool(account.get("is_suspended", False)),
            )
            if is_suspended_now:
                log_and_telegram(f"[{username}] ⛔ Account suspended, skipping account")
                continue

            is_enabled_now = database.is_account_automation_enabled(
                username,
                default=bool(account.get("automation_enabled", True)),
            )
            if not is_enabled_now:
                log_and_telegram(f"[{username}] 👁️‍🗨️ Automation disabled, skipping account")
                continue

            account_lock_acquired = False
            if coordinator and coordinator.enabled:
                owner_for_lock = (
                    str(account.get("owner_username", "")).strip().lower()
                    or str(account_owner or "").strip().lower()
                    or "master"
                )
                account_lock_acquired, lock_reason = coordinator.acquire_account_lock(
                    username=username,
                    owner_username=owner_for_lock,
                    session_id=distributed_session_id,
                )
                if not account_lock_acquired:
                    if lock_reason == "already_locked":
                        log_and_telegram(f"[{username}] ⏭️ Account locked by another VPS, skipping")
                    else:
                        log_and_telegram(
                            f"[{username}] ⚠️ Could not acquire distributed lock ({lock_reason}), skipping for safety"
                        )
                    continue

            account_model_key = _normalize_account_model_label(account.get("model_label", ""))
            account_models = _models_for_account(account, models)
            
            account_custom_messages = _normalize_message_list(account.get("custom_messages"))
            account_label_display = label_display

            log_and_telegram(f"━━━ Switching to account: @{username} ━━━")
            if account_model_key:
                log_and_telegram(f"[{username}] 🏷️ Marketing label: {account_label_display}")
            else:
                log_and_telegram(f"[{username}] 🏷️ Marketing label: Generic")

            telegram_bot.stats["current_account"] = username
            telegram_bot.stats["accounts_used"] += 1

            account_dm_batch_state = {
                "sent_count": 0,
                "last_cooldown_count": 0,
                "sent_since_human_break": 0,
            }

            # Create browser + login with proxy failover (up to MAX_ACCOUNT_PROXIES)
            driver = None
            logged_in = False
            try:
                proxy_candidates = _normalize_account_proxy_candidates(account.get("proxy", ""))
                connection_candidates = list(proxy_candidates) if proxy_candidates else [None]

                if proxy_candidates:
                    proxy_preview = ", ".join(_mask_proxy_for_log(proxy) for proxy in proxy_candidates)
                    log_and_telegram(
                        f"[{username}] 🌐 Proxy pool loaded ({len(proxy_candidates)}/{MAX_ACCOUNT_PROXIES}): {proxy_preview}"
                    )
                else:
                    log_and_telegram(f"[{username}] 🌐 No proxy configured, using direct connection")

                for attempt_index, candidate_proxy in enumerate(connection_candidates, start=1):
                    if stop_event and stop_event.is_set():
                        break

                    try:
                        if candidate_proxy:
                            log_and_telegram(
                                f"[{username}] 🌐 Attempt {attempt_index}/{len(connection_candidates)} with proxy: "
                                f"{_mask_proxy_for_log(candidate_proxy)}"
                            )
                        else:
                            log_and_telegram(
                                f"[{username}] 🌐 Attempt {attempt_index}/{len(connection_candidates)} with direct connection"
                            )

                        driver = create_driver(headless=False, proxy=candidate_proxy)
                        _register_driver(driver)
                    except Exception as e:
                        error_text = str(e).strip() or repr(e)
                        log_and_telegram(
                            f"❌ Failed to create browser for @{username} on attempt "
                            f"{attempt_index}/{len(connection_candidates)}: {error_text}"
                        )
                        if attempt_index == len(connection_candidates):
                            log_and_telegram(
                                "⚠️ Browser bootstrap failed. Auto ChromeDriver download may be blocked on this VPS."
                            )
                            log_and_telegram(
                                "💡 Tip: install a matching ChromeDriver binary and set CHROMEDRIVER_PATH for this host."
                            )
                        continue

                    try:
                        logged_in = _perform_login(driver, account)
                    except Exception as e:
                        logged_in = False
                        log_and_telegram(
                            f"❌ Login error for @{username} on attempt "
                            f"{attempt_index}/{len(connection_candidates)}: {e}"
                        )

                    if logged_in:
                        break

                    log_and_telegram(
                        f"⚠️ Login failed for @{username} on attempt "
                        f"{attempt_index}/{len(connection_candidates)}"
                    )
                    close_driver(driver)
                    _unregister_driver(driver)
                    driver = None

                if not logged_in or not driver:
                    log_and_telegram(
                        f"❌ Failed to login @{username} after trying {len(connection_candidates)} connection option(s), skipping"
                    )
                    if account_lock_acquired and coordinator:
                        coordinator.release_account_lock(username)
                    continue

                # ── Sequential Model Loop (DM mode) ──
                for model_username in account_models:
                    _maybe_send_24h_dm_summary(hours=DM_SUMMARY_WINDOW_HOURS)

                    if stop_event and stop_event.is_set():
                        log_and_telegram("🛑 Stop requested, breaking model loop.")
                        break

                    if coordinator and coordinator.enabled and not coordinator.has_account_lock(username):
                        log_and_telegram(f"[{username}] ⚠️ Distributed lock lost, stopping account session")
                        break

                    if not telegram_bot._polling:
                        log_and_telegram("🛑 Stop requested, finishing up...")
                        break

                    log_and_telegram(f"🎯 Targeting model: @{model_username}")
                    telegram_bot.stats["current_model"] = model_username

                    model_key = _normalize_model_key(model_username) or str(model_username or "").strip().lower()

                    # Default DM/Model flow
                    custom_messages = model_message_map.get(_normalize_model_key(model_username), [])
                    if account_model_key and account_custom_messages:
                        messages_for_model = account_custom_messages
                        log_and_telegram(
                            f"[{username}] Using {len(messages_for_model)} account custom messages for @{model_username}"
                        )
                    elif account_model_key:
                        messages_for_model = custom_messages if custom_messages else messages
                        if not messages_for_model:
                            log_and_telegram(
                                f"[{username}] ⚠️ No generic messages configured for @{model_username}, skipping"
                            )
                            continue
                    else:
                        messages_for_model = custom_messages if custom_messages else messages
                        if not messages_for_model:
                            log_and_telegram(f"[{username}] ⚠️ No messages configured for @{model_username}, skipping")
                            continue

                    dms_for_model = _process_model(
                        driver,
                        account,
                        model_username,
                        messages_for_model,
                        dm_log,
                        already_dmd,
                        dm_batch_state=account_dm_batch_state,
                        stop_event=stop_event,
                        coordinator=coordinator,
                        use_global_target_dedupe=use_global_target_dedupe,
                    )

                    total_dms_sent += dms_for_model
                    telegram_bot.stats["dms_sent"] = total_dms_sent

                    if dms_for_model > 0:
                        session_account_dm_counts[username] = int(session_account_dm_counts.get(username, 0)) + int(dms_for_model)
                        if model_key:
                            completed_model_keys.add(model_key)
                        telegram_bot.stats["models_processed"] = len(completed_model_keys)
                        telegram_bot.send_model_complete(model_username, dms_for_model, sender_account=username)

                    # Account limit break
                    account_dms_so_far = session_account_dm_counts.get(username, 0)
                    if account_dms_so_far >= random.randint(10, 15):
                        log_and_telegram(f"[{username}] 🛑 Reached account limits (10-15 DMs). Switching to next account.")
                        break

                    # Check if still logged in
                    if not is_logged_in(driver):
                        log_and_telegram(f"⚠️ Lost login for @{username} during model processing")
                        break

                    # Delay before next model
                    model_delay_min = _setting_float("MODEL_SWITCH_DELAY_MIN")
                    model_delay_max = _setting_float("MODEL_SWITCH_DELAY_MAX")
                    if model_delay_max < model_delay_min:
                        model_delay_min, model_delay_max = model_delay_max, model_delay_min

                    delay = random.uniform(model_delay_min, model_delay_max)
                    log_and_telegram(f"⏳ Waiting {delay:.0f}s before next model...")
                    if _interruptible_sleep(delay, stop_event=stop_event):
                        break

                # Refresh cookies after session
                refresh_cookies(driver, username)

            except Exception as e:
                if stop_event and stop_event.is_set() and _is_expected_driver_shutdown_error(e):
                    log_and_telegram(f"🛑 Stop requested while closing @{username} browser session")
                else:
                    log_and_telegram(f"❌ Error with @{username}: {e}")
                    telegram_bot.send_error(str(e))
            finally:
                close_driver(driver)
                _unregister_driver(driver)
                if account_lock_acquired and coordinator:
                    coordinator.release_account_lock(username)

            # Delay before switching accounts
            if account_index < len(accounts) - 1 and not (stop_event and stop_event.is_set()):
                account_delay_min = _setting_float("ACCOUNT_SWITCH_DELAY_MIN")
                account_delay_max = _setting_float("ACCOUNT_SWITCH_DELAY_MAX")
                if account_delay_max < account_delay_min:
                    account_delay_min, account_delay_max = account_delay_max, account_delay_min

                delay = random.uniform(account_delay_min, account_delay_max)
                log_and_telegram(f"⏳ Waiting {delay:.0f}s before switching accounts...")
                if _interruptible_sleep(delay, stop_event=stop_event):
                    break

        if active_label_key is not None and not (stop_event and stop_event.is_set()):
            log_and_telegram(f"✅ Finished label batch: {active_label_display}")

    except KeyboardInterrupt:
        log_and_telegram("🛑 Bot stopped by user (Ctrl+C)")
    except Exception as e:
        if stop_event and stop_event.is_set() and _is_expected_driver_shutdown_error(e):
            log_and_telegram("🛑 Stop requested. Browser connections were terminated.")
        else:
            log_and_telegram(f"❌ Fatal error: {e}")
            telegram_bot.send_error(str(e))
    finally:
        if coordinator:
            try:
                coordinator.shutdown()
            except Exception as e:
                logger.debug(f"Failed to shutdown distributed coordinator cleanly: {e}")

        _maybe_send_24h_dm_summary(hours=DM_SUMMARY_WINDOW_HOURS)
        should_send_session_complete = not continuous_mode
        if stop_event and stop_event.is_set():
            should_send_stop_notice = _claim_cluster_notification(
                event_name="bot_stop",
                expected_state="stopped",
            )
            if continuous_mode:
                if should_send_stop_notice:
                    telegram_bot.send("🛑 *BOT STOPPED*")
                else:
                    logger.info("Skipping duplicate cluster stop Telegram notification on this VPS")
            else:
                should_send_session_complete = should_send_stop_notice

        if should_send_session_complete:
            telegram_bot.send_session_complete(
                total_dms_sent,
                len(completed_model_keys),
                by_account=session_account_dm_counts,
            )
        elif stop_event and stop_event.is_set() and not continuous_mode:
            logger.info("Skipping duplicate cluster stop Telegram notification on this VPS")

        telegram_bot.stats["current_account"] = "—"
        telegram_bot.stats["current_model"] = "—"
        telegram_bot.stats["status"] = "Stopped"

    logger.info("=" * 60)
    completion_label = "PASS COMPLETE" if continuous_mode else "SESSION COMPLETE"
    logger.info(f"  {completion_label} — {total_dms_sent} DMs sent, {len(completed_model_keys)} unique models done")
    logger.info("=" * 60)


def _perform_login(driver, account: dict) -> bool:
    """
    Attempt login: cookies first, then credentials, handle challenges.
    """
    username = account["username"]

    # Try cookie login
    if login_with_cookies(driver, account):
        return True

    # Check if cookie login failed because it hit a challenge
    challenge = detect_challenge(driver)
    if challenge != ChallengeType.NONE:
        logger.warning(f"[{username}] Challenge detected after cookie injection, skipping credential login.")
    else:
        # Only try credential login if no challenge is blocking us
        if login_with_credentials(driver, account):
            return True
        
    # Final check for challenges (from either cookie or credential login)
    challenge = detect_challenge(driver)

    if challenge == ChallengeType.NONE:
        return False

    log_and_telegram(f"🔒 Challenge for @{username}: {challenge.value}")
    telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
    if challenge == ChallengeType.LOCKED:
        telegram_bot.send_lockout_alert(username, "Account locked during login")
        _mark_account_suspended(username, "locked during login")
        log_and_telegram(f"⏭️ @{username} is locked. Skipping account automatically.")
        return False

    if challenge in (ChallengeType.TWO_FACTOR, ChallengeType.SUSPICIOUS_LOGIN, ChallengeType.CHECKPOINT):
        log_and_telegram(
            f"⏭️ @{username} challenge ({challenge.value}) is auto-skipped. Continuing with next account."
        )
        return False

    return False


def _process_model(
    driver, account: dict, model_username: str,
    messages: list, dm_log: dict, already_dmd: set,
    dm_batch_state: dict = None,
    stop_event=None,
    coordinator=None,
    use_global_target_dedupe: bool = False,
) -> int:
    """
    Process a single model target using the injected JS userscript.
    """
    import os, time, json
    
    username = account.get("username", "unknown")
    dm_min = _setting_int("DM_MIN_PER_MODEL")
    dm_max = _setting_int("DM_MAX_PER_MODEL")
    if dm_max < dm_min:
        dm_min, dm_max = dm_max, dm_min
    dm_target = random.randint(dm_min, dm_max)
    
    # Send config directly into JS
    try:
        telegram_chat_ids = database.get_setting("TELEGRAM_CHAT_IDS", [])
    except Exception:
        telegram_chat_ids = []
        
    if isinstance(telegram_chat_ids, list):
        telegram_chat_ids_str = ", ".join(str(x) for x in telegram_chat_ids)
    else:
        telegram_chat_ids_str = str(telegram_chat_ids)
        
    run_config = {
        "senderUsername": username,
        "modelName": model_username,
        "targetCount": dm_target,
        "useTelegram": False,  # Python handles Telegram alerts
        "messageTemplates": messages,
        "telegramChatIds": telegram_chat_ids_str,
        "lockAlertBotToken": database.get_setting("LOCK_ALERT_BOT_TOKEN", "")
    }
    
    run_config_json = json.dumps(run_config)
    config_script = f"""
    sessionStorage.setItem('scraper_config', JSON.stringify({run_config_json}));
    sessionStorage.setItem('scraper_state', JSON.stringify({{
        step: 'navigate_to_profile',
        modelName: '{model_username}'
    }}));
    sessionStorage.removeItem('dmProcessedFollowers');
    // Important: we wait a bit before navigating so that if the script was just loaded, it can set listeners
    setTimeout(() => {{
        window.location.href = 'https://www.instagram.com/{model_username}/';
    }}, 1000);
    """
    
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send-dms-script.js")
    js_content = ""
    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()
    else:
        log_and_telegram(f"❌ [JS Scraper] Cannot find `{js_path}`")
        return 0
        
    # Start tracking dms sent
    try:
        start_dms_val = driver.execute_script(f"return localStorage.getItem('ig_auto_dm_sent_count_this_run_v1_{username}');")
        start_dms = int(start_dms_val) if start_dms_val else 0
    except:
        start_dms = 0

    log_and_telegram(f"[{username}] 🚀 Launching JS scraper for @{model_username} (Target: {dm_target} DMs)...")

    # Inject the script on new document navigation (keeps it persistent)
    try:
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': js_content})
    except Exception as e:
        logger.debug(f"Failed CDP injection for JS userscript: {e}")

    # Also inject right now to guarantee it registers
    try:
        driver.execute_script(js_content)
    except Exception:
        pass

    # Start the scraper loop by hitting the profile page with the session vars
    try:
        driver.execute_script(config_script)
    except Exception as e:
        log_and_telegram(f"❌ [JS Scraper] Failed to configure/start script: {e}")
        return 0
        
    last_state = None    
    # Wait until JS signals completion
    while True:
        if stop_event and stop_event.is_set():
            log_and_telegram(f"[{username}] 🛑 Stopping JS scraper early.")
            try:
                driver.execute_script("sessionStorage.removeItem('scraper_state'); sessionStorage.removeItem('auto_dm_queue');")
            except:
                pass
            break
            
        time.sleep(4)
        
        # Check if Instagram threw a suspension or challenge during the automated scraping
        challenge = detect_challenge(driver)
        if challenge != ChallengeType.NONE:
            log_and_telegram(f"🚨 Account @{username} hit a challenge ({challenge.value}) while DMing followers!")
            telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
            if challenge == ChallengeType.LOCKED:
                telegram_bot.send_lockout_alert(username, "Account locked/suspended during DM scraping")
                _mark_account_suspended(username, "locked during DM scraping")
            
            # Stop the JS scraper by clearing its state
            try:
                driver.execute_script("sessionStorage.removeItem('scraper_state'); sessionStorage.removeItem('auto_dm_queue');")
            except:
                pass
            break

        try:
            state = driver.execute_script("return sessionStorage.getItem('scraper_state');")
            if not state:
                # Polling indicates it finished or cleared its state
                # In Tampermonkey script: "setScrapingState(false); sessionStorage.removeItem('scraper_state');"
                # But wait, it might reload the page. Let's make sure it's actually finished.
                # We will check if `auto_dm_queue` and `scraper_state` are both null.
                state_dm = driver.execute_script("return sessionStorage.getItem('auto_dm_queue');")
                if not state and not state_dm:
                    break
        except Exception:
            # Maybe loading a new page, ignore error and wait
            pass
            
    # Calculate how many were sent
    try:
        end_dms_val = driver.execute_script(f"return localStorage.getItem('ig_auto_dm_sent_count_this_run_v1_{username}');")
        end_dms = int(end_dms_val) if end_dms_val else 0
        sent_this_round = max(0, end_dms - start_dms)
    except:
        sent_this_round = 0
        
    log_and_telegram(f"[{username}] ✅ JS scraper finished for @{model_username}. Sent {sent_this_round} DMs.")
    return sent_this_round


def _close_liker_driver(session_cache: dict, username: str):
    driver = session_cache.pop(username, None)
    if not driver:
        return
    try:
        close_driver(driver)
    finally:
        _unregister_driver(driver)


def _cleanup_liker_drivers(session_cache: dict):
    for username in list(session_cache.keys()):
        _close_liker_driver(session_cache, username)


def _get_liker_driver(session_cache: dict, account: dict):
    username = str(account.get("username") or "").strip()
    if not username:
        return None

    # Enforce a single active liker browser at a time.
    for other_username in list(session_cache.keys()):
        if other_username != username:
            _close_liker_driver(session_cache, other_username)

    driver = session_cache.get(username)
    if driver:
        try:
            _ = driver.current_url
        except Exception:
            _close_liker_driver(session_cache, username)
            driver = None

    if driver is None:
        proxy = account.get("proxy")
        driver = create_driver(proxy=proxy)
        _register_driver(driver)
        session_cache[username] = driver

    return driver


def run_liker_bot(stop_event=None, account_owner=None, continuous_mode=False, state_obj=None):
    """
    Main entry point for the Comment Liker bot.
    Rotates accounts and processes target models for comment liking.
    """
    database.init_db()
    coordinator = DistributedCoordinator.from_settings(
        database.get_all_settings(), 
        logger=logger, 
        account_owner=account_owner
    )
    pass_num = 0
    session_cache = {}
    preferred_account_username = ""

    if coordinator.enabled and not coordinator.is_active:
        mode_label = "fail-closed" if coordinator.fail_closed else "best-effort"
        logger.warning(
            "Distributed coordination enabled but Redis is unavailable (%s mode).",
            mode_label,
        )

    telegram_bot.start_polling()
    should_send_startup_bundle = _claim_cluster_notification(
        event_name="comment_liker_start",
        expected_state="running",
    )
    if should_send_startup_bundle:
        start_msg = "🚀 *COMMENT LIKER BOT STARTED*"
        telegram_bot.send_startup(start_msg)
        try:
            summary_accounts = database.get_accounts(
                owner_username=account_owner,
                include_all=(account_owner is None),
            )
            summary_accounts = [
                acc for acc in summary_accounts
                if not acc.get("is_suspended") and acc.get("automation_enabled")
            ]
            summary_models = database.get_models()
            telegram_bot.send_account_pool_summary(
                _build_account_pool_summary(summary_accounts, summary_models)
            )
            telegram_bot.send_account_profile_summary(
                summary_accounts,
                limit=3,
                recent_only=True,
            )
        except Exception as e:
            logger.warning(f"Failed to send comment liker startup bundle: {e}")
    else:
        logger.info("Skipping duplicate comment liker startup Telegram notification on this VPS")

    try:
        if state_obj:
            state_obj["status"] = "Comment Liker Running"
        telegram_bot.stats["status"] = "Comment Liker Running"
        while not (stop_event and stop_event.is_set()):
            pass_num += 1
            logger.info(f"❤️ LIKER PASS #{pass_num} STARTING")
            
            models = database.get_models()
            if not models:
                telegram_bot.stats["status"] = "Waiting for models..."
                logger.info("No models to process for liking. Waiting 5 minutes...")
                if stop_event.wait(300): break
                continue

            model_pool = list(models)

            while model_pool:
                model_username = _pop_random_item(model_pool)
                if stop_event and stop_event.is_set(): break
                
                if not model_username: continue

                if state_obj:
                    state_obj["current_model"] = f"@{model_username}"
                    state_obj["status"] = f"Liking comments for @{model_username}"
                
                telegram_bot.stats["current_model"] = f"@{model_username}"
                telegram_bot.stats["status"] = f"Liking comments for @{model_username}"
                logger.info(f"🎯 Processing Liker for @{model_username}")
                
                # Find eligible accounts
                accounts = database.get_accounts(owner_username=account_owner, include_all=(account_owner is None))
                accounts = [a for a in accounts if not a.get("is_suspended") and a.get("automation_enabled")]
                if not accounts:
                    logger.warning("No automation-enabled accounts available for Comment Liker.")
                    if stop_event.wait(300):
                        break
                    continue

                account_pool = list(accounts)
                total_accounts = len(account_pool)

                success = False
                no_posts_found = False
                lock_skips = 0
                lock_reasons = {}
                while account_pool:
                    if stop_event and stop_event.is_set(): break

                    acc = _pop_random_item(account_pool)
                    if not acc:
                        continue
                    
                    username = acc.get("username")
                    if state_obj:
                        state_obj["current_account"] = f"@{username}"
                    telegram_bot.stats["current_account"] = f"@{username}"
                    
                    if coordinator and coordinator.enabled:
                        account_lock_acquired, lock_reason = coordinator.acquire_account_lock(
                            username=username,
                            owner_username=account_owner or "master"
                        )
                        if not account_lock_acquired:
                            lock_skips += 1
                            lock_reasons[lock_reason] = lock_reasons.get(lock_reason, 0) + 1
                            continue
                    else:
                        account_lock_acquired = False

                    try:
                        telegram_bot.stats["accounts_used"] += 1
                        driver = _get_liker_driver(session_cache, acc)
                        if not driver:
                            continue

                        result = _process_model_liker(
                            acc,
                            model_username,
                            stop_event,
                            state_obj=state_obj,
                            driver=driver,
                        )

                        if result.get("should_close"):
                            _close_liker_driver(session_cache, str(username or "").strip())
                            if str(username or "").strip().lower() == preferred_account_username.lower():
                                preferred_account_username = ""

                        if result.get("found_posts") is False:
                            no_posts_found = True
                            preferred_account_username = str(username or "").strip()
                            break

                        likes_given = int(result.get("likes", 0) or 0)
                        if likes_given > 0:
                            success = True
                            preferred_account_username = ""
                            telegram_bot.stats["models_processed"] += 1
                            telegram_bot.send_liker_model_complete(model_username, likes_given, username)
                            break
                    except Exception as e:
                        logger.error(f"Error processing @{model_username} with @{username}: {e}")
                        continue
                    finally:
                        if account_lock_acquired:
                            coordinator.release_account_lock(username)

                if no_posts_found:
                    logger.info(f"No posts/reels found for @{model_username}. Skipping to next model.")
                    continue

                if not success and lock_skips >= total_accounts:
                    reason_summary = ", ".join(
                        f"{reason}={count}" for reason, count in lock_reasons.items()
                    ) or "unknown"
                    logger.warning(
                        f"All accounts locked/unavailable for @{model_username} ({reason_summary})."
                    )

                if not success:
                    logger.warning(f"Could not process @{model_username} with any available account.")

            if not continuous_mode:
                break
                
            logger.info(f"✅ Liker Pass #{pass_num} finished. Waiting for next cycle...")
            if stop_event.wait(600): break # Wait 10 mins between passes
    finally:
        if coordinator:
            coordinator.shutdown()
        if state_obj:
            state_obj["status"] = "stopped"
        _cleanup_liker_drivers(session_cache)
        if stop_event and stop_event.is_set():
            should_send_stop_notice = _claim_cluster_notification(
                event_name="comment_liker_stop",
                expected_state="stopped",
            )
            if should_send_stop_notice:
                telegram_bot.send("BOT STOPPED")
            else:
                logger.info("Skipping duplicate comment liker stop Telegram notification on this VPS")

        log_and_telegram("🛑 Comment Liker Bot stopped.")


def _process_model_liker(account, model_username, stop_event=None, state_obj=None, driver=None):
    """Run the liking process for a single model with a single account."""
    username = account.get("username")

    if driver is None:
        return {"likes": 0, "found_posts": True, "should_close": True}

    # Login (reuse session when already logged in)
    logged_in = is_logged_in(driver)
    if not logged_in:
        logged_in = login_with_cookies(driver, account)
    if not logged_in:
        logged_in = login_with_credentials(driver, account)

    # Post-login check for suspension or challenges
    challenge = detect_challenge(driver)
    current_url = driver.current_url.lower()

    # Handle successful challenge redirects (Soft Checkpoint)
    if "__coig_challenge_redirected=1" in current_url or "logged_in_redirect" in current_url:
        log_and_telegram(f"🔄 Account @{username} hit a soft challenge but is being redirected. Waiting for home page...")
        telegram_bot.send_lockout_alert(username, "Soft checkpoint hit - Redirecting to Home. Bot will continue.")
        # Wait for redirect to finish
        time.sleep(10)
        challenge = detect_challenge(driver) # Re-check after redirect

    if challenge == ChallengeType.LOCKED:
        log_and_telegram(f"🚨 Account @{username} is SUSPENDED or LOCKED!")
        telegram_bot.send_lockout_alert(username, "Account suspended detected during Liker login")
        _mark_account_suspended(username, "suspended detected during Liker login")
        return {"likes": 0, "found_posts": True, "should_close": True}
    elif challenge != ChallengeType.NONE:
        log_and_telegram(f"⚠️ Account @{username} hit a challenge ({challenge.value}) during Liker login")
        telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
        return {"likes": 0, "found_posts": True, "should_close": True}

    if not logged_in:
        logger.error(f"Failed to log in to @{username}")
        return {"likes": 0, "found_posts": True, "should_close": True}

    # Settings
    posts_count = _setting_int("LIKE_POSTS_COUNT")
    likes_per_post = _setting_int("LIKE_PER_POST_COUNT")

    if posts_count <= 0:
        posts_count = None
    if likes_per_post <= 0:
        likes_per_post = None

    likes_logged = 0
    likes_seen = 0

    def on_like_callback(post_url):
        nonlocal likes_logged, likes_seen
        try:
            database.log_like_event(username, model_username, post_url, 1)
            likes_logged += 1
        except Exception as e:
            logger.debug(f"Failed to log like event for @{model_username}: {e}")

        likes_seen += 1
        if likes_seen % 100 == 0:
            safe_model = str(model_username or "").strip().lstrip("@") or "unknown"
            safe_user = str(username or "").strip().lstrip("@") or "unknown"
            telegram_bot.send(
                "COMMENT LIKER UPDATE\n\n"
                f"Model: `@{safe_model}`\n"
                f"Account: `@{safe_user}`\n"
                f"Comments liked: {likes_seen}"
            )

        telegram_bot.stats["likes_given"] += 1
        if state_obj:
            state_obj["likes_today"] = state_obj.get("likes_today", 0) + 1
            state_obj["total_likes_all_time"] = state_obj.get("total_likes_all_time", 0) + 1
            state_obj["last_like_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    result = run_comment_liker_script(
        driver,
        model_username,
        posts_count,
        likes_per_post,
        stop_event,
        on_like=on_like_callback,
    )

    total_liked = int(result.get("total_liked", 0) or 0)
    found_posts = bool(result.get("found_posts", True))

    if total_liked > likes_logged:
        missing_likes = total_liked - likes_logged
        try:
            database.log_like_event(username, model_username, f"batch://{model_username}", missing_likes)
        except Exception as e:
            logger.debug(f"Failed to log batch like events for @{model_username}: {e}")
    
    # Final check after liking process in case suspension happened during the run
    challenge = detect_challenge(driver)
    if challenge == ChallengeType.LOCKED:
        log_and_telegram(f"🚨 Account @{username} was SUSPENDED during the liking process!")
        telegram_bot.send_lockout_alert(username, "Account suspended during comment liking")
        _mark_account_suspended(username, "suspended during comment liking")
        return {"likes": total_liked, "found_posts": found_posts, "should_close": True}
        
    return {"likes": total_liked, "found_posts": found_posts, "should_close": False}


def run_story_liker_bot(stop_event=None, account_owner=None, continuous_mode=False, state_obj=None):
    """
    Main entry point for the Story Liker bot.
    Rotates accounts and processes target models for story liking.
    """
    database.init_db()
    coordinator = DistributedCoordinator.from_settings(
        database.get_all_settings(),
        logger=logger,
        account_owner=account_owner,
    )
    pass_num = 0
    session_cache = {}
    preferred_account_username = ""

    try:
        if state_obj:
            state_obj["status"] = "Story Liker Running"
        telegram_bot.stats["status"] = "Story Liker Running"

        telegram_bot.start_polling()
        should_send_startup_bundle = _claim_cluster_notification(
            event_name="story_liker_start",
            expected_state="running",
        )
        if should_send_startup_bundle:
            start_msg = "🚀 *STORY LIKER BOT STARTED*"
            telegram_bot.send_startup(start_msg)
            try:
                summary_accounts = database.get_accounts(
                    owner_username=account_owner,
                    include_all=(account_owner is None),
                )
                summary_accounts = [
                    acc for acc in summary_accounts
                    if not acc.get("is_suspended") and acc.get("automation_enabled")
                ]
                summary_models = database.get_models()
                telegram_bot.send_account_pool_summary(
                    _build_account_pool_summary(summary_accounts, summary_models)
                )
                telegram_bot.send_account_profile_summary(
                    summary_accounts,
                    limit=3,
                    recent_only=True,
                )
            except Exception as e:
                logger.warning(f"Failed to send story liker startup bundle: {e}")
        else:
            logger.info("Skipping duplicate story liker startup Telegram notification on this VPS")

        while not (stop_event and stop_event.is_set()):
            pass_num += 1
            logger.info(f"🎬 STORY LIKER PASS #{pass_num} STARTING")

            followers_limit = _setting_int_default(
                "STORY_LIKE_MAX_FOLLOWERS",
                _setting_int("MAX_FOLLOWERS_TO_SCRAPE"),
            )
            if followers_limit <= 0:
                followers_limit = None

            account_like_target = _setting_int_default("STORY_LIKE_LIKES_PER_ACCOUNT", 100)
            if account_like_target <= 0:
                account_like_target = 100

            models = database.get_models()
            if not models:
                telegram_bot.stats["status"] = "Waiting for models..."
                logger.info("No models to process for story liking. Waiting 5 minutes...")
                if stop_event.wait(300):
                    break
                continue

            accounts = database.get_accounts(owner_username=account_owner, include_all=(account_owner is None))
            accounts = [a for a in accounts if not a.get("is_suspended") and a.get("automation_enabled")]
            if not accounts:
                logger.warning("No automation-enabled accounts available for Story Liker.")
                if stop_event.wait(300):
                    break
                continue

            account_pool = list(accounts)

            while account_pool:
                if stop_event and stop_event.is_set():
                    break

                acc = _pop_random_item(account_pool)
                if not acc:
                    continue

                username = acc.get("username")
                if state_obj:
                    state_obj["current_account"] = f"@{username}"
                telegram_bot.stats["current_account"] = f"@{username}"

                if coordinator and coordinator.enabled:
                    account_lock_acquired, lock_reason = coordinator.acquire_account_lock(
                        username=username,
                        owner_username=account_owner or "master",
                    )
                    if not account_lock_acquired:
                        continue
                else:
                    account_lock_acquired = False

                try:
                    telegram_bot.stats["accounts_used"] += 1
                    driver = _get_liker_driver(session_cache, acc)
                    if not driver:
                        continue

                    logged_in = is_logged_in(driver)
                    if not logged_in:
                        logged_in = login_with_cookies(driver, acc)
                    if not logged_in:
                        logged_in = login_with_credentials(driver, acc)

                    challenge = detect_challenge(driver)
                    current_url = driver.current_url.lower()
                    if "__coig_challenge_redirected=1" in current_url or "logged_in_redirect" in current_url:
                        log_and_telegram(
                            f"🔄 Account @{username} hit a soft challenge but is being redirected. "
                            "Waiting for home page..."
                        )
                        telegram_bot.send_lockout_alert(username, "Soft checkpoint hit - Redirecting to Home. Bot will continue.")
                        time.sleep(10)
                        challenge = detect_challenge(driver)

                    if challenge == ChallengeType.LOCKED:
                        log_and_telegram(f"🚨 Account @{username} is SUSPENDED or LOCKED!")
                        telegram_bot.send_lockout_alert(username, "Account suspended detected during Story Liker login")
                        _mark_account_suspended(username, "suspended detected during Story Liker login")
                        _close_liker_driver(session_cache, str(username or "").strip())
                        continue
                    elif challenge != ChallengeType.NONE:
                        log_and_telegram(
                            f"⚠️ Account @{username} hit a challenge ({challenge.value}) during Story Liker login"
                        )
                        telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
                        continue

                    if not logged_in:
                        logger.error(f"Failed to log in to @{username}")
                        continue

                    likes_for_account = 0
                    cycle_num = 0
                    models_completed = set()
                    seen_followers_by_model = {}
                    account_should_close = False

                    while likes_for_account < account_like_target and not (stop_event and stop_event.is_set()):
                        cycle_num += 1
                        likes_at_cycle_start = likes_for_account

                        model_pool = list(models)
                        while model_pool:
                            model_username = _pop_random_item(model_pool)
                            if stop_event and stop_event.is_set():
                                break
                            if likes_for_account >= account_like_target:
                                break
                            if not model_username:
                                continue

                            if state_obj:
                                state_obj["current_model"] = f"@{model_username}"
                                state_obj["status"] = f"Liking follower stories for @{model_username}"

                            telegram_bot.stats["current_model"] = f"@{model_username}"
                            telegram_bot.stats["status"] = f"Liking follower stories for @{model_username}"
                            logger.info(f"🎯 Processing Story Liker for @{model_username} followers")

                            model_key = str(model_username or "").strip().lower()
                            seen_followers = seen_followers_by_model.setdefault(model_key, set())
                            followers = get_followers(
                                driver,
                                model_username,
                                seen_followers,
                                max_count=followers_limit,
                            )
                            if not followers:
                                logger.info(
                                    f"No followers found for @{model_username} with @{username}."
                                )
                                continue

                            followers = list(followers)
                            random.shuffle(followers)

                            seen_followers.update(followers)

                            likes_for_model = 0
                            any_story_found = False

                            for target_username in followers:
                                if stop_event and stop_event.is_set():
                                    break
                                if likes_for_account >= account_like_target:
                                    break

                                if state_obj:
                                    state_obj["status"] = (
                                        f"Liking stories for @{target_username} (from @{model_username})"
                                    )
                                telegram_bot.stats["status"] = (
                                    f"Liking stories for @{target_username} (from @{model_username})"
                                )

                                result = _process_model_story_liker(
                                    acc,
                                    model_username,
                                    target_username=target_username,
                                    stop_event=stop_event,
                                    state_obj=state_obj,
                                    driver=driver,
                                )

                                if result.get("should_close"):
                                    _close_liker_driver(session_cache, str(username or "").strip())
                                    account_should_close = True
                                    break

                                likes_gained = int(result.get("likes", 0) or 0)
                                likes_for_model += likes_gained
                                likes_for_account += likes_gained
                                if result.get("found_story"):
                                    any_story_found = True

                            if account_should_close:
                                break

                            if not any_story_found:
                                logger.info(
                                    f"No stories found for followers of @{model_username} with @{username}."
                                )

                            if likes_for_model > 0 and model_key not in models_completed:
                                models_completed.add(model_key)
                                telegram_bot.stats["models_processed"] += 1
                                telegram_bot.send_story_liker_model_complete(model_username, likes_for_model, username)

                        if account_should_close or (stop_event and stop_event.is_set()):
                            break

                        if likes_for_account == likes_at_cycle_start:
                            logger.info(
                                f"No story likes added for @{username} in cycle {cycle_num}. Switching to next account."
                            )
                            break

                    if likes_for_account >= account_like_target:
                        logger.info(
                            f"@{username} reached {account_like_target} story likes. Switching to next account."
                        )
                except Exception as e:
                    logger.error(f"Error processing story liker for @{username}: {e}")
                    continue
                finally:
                    if account_lock_acquired:
                        coordinator.release_account_lock(username)

            if not continuous_mode:
                break

            logger.info(f"✅ Story Liker Pass #{pass_num} finished. Waiting for next cycle...")
            if stop_event.wait(600):
                break
    finally:
        if coordinator:
            coordinator.shutdown()
        if state_obj:
            state_obj["status"] = "stopped"
        _cleanup_liker_drivers(session_cache)
        if stop_event and stop_event.is_set():
            should_send_stop_notice = _claim_cluster_notification(
                event_name="story_liker_stop",
                expected_state="stopped",
            )
            if should_send_stop_notice:
                telegram_bot.send("BOT STOPPED")
            else:
                logger.info("Skipping duplicate story liker stop Telegram notification on this VPS")
        log_and_telegram("🛑 Story Liker Bot stopped.")


def _process_model_story_liker(
    account,
    model_username,
    target_username=None,
    stop_event=None,
    state_obj=None,
    driver=None,
):
    """Run the story liking process for a single target profile with a single account."""
    username = account.get("username")
    model_username = str(model_username or "").strip().lstrip("@")
    target_username = str(target_username or model_username or "").strip().lstrip("@")

    if not target_username:
        return {"likes": 0, "found_story": True, "should_close": False}

    if driver is None:
        return {"likes": 0, "found_story": True, "should_close": True}

    logged_in = is_logged_in(driver)
    if not logged_in:
        logged_in = login_with_cookies(driver, account)
    if not logged_in:
        logged_in = login_with_credentials(driver, account)

    challenge = detect_challenge(driver)
    current_url = driver.current_url.lower()

    if "__coig_challenge_redirected=1" in current_url or "logged_in_redirect" in current_url:
        log_and_telegram(
            f"🔄 Account @{username} hit a soft challenge but is being redirected. Waiting for home page..."
        )
        telegram_bot.send_lockout_alert(username, "Soft checkpoint hit - Redirecting to Home. Bot will continue.")
        time.sleep(10)
        challenge = detect_challenge(driver)

    if challenge == ChallengeType.LOCKED:
        log_and_telegram(f"🚨 Account @{username} is SUSPENDED or LOCKED!")
        telegram_bot.send_lockout_alert(username, "Account suspended detected during Story Liker login")
        _mark_account_suspended(username, "suspended detected during Story Liker login")
        return {"likes": 0, "found_story": True, "should_close": True}
    elif challenge != ChallengeType.NONE:
        log_and_telegram(
            f"⚠️ Account @{username} hit a challenge ({challenge.value}) during Story Liker login"
        )
        telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
        return {"likes": 0, "found_story": True, "should_close": True}

    if not logged_in:
        logger.error(f"Failed to log in to @{username}")
        return {"likes": 0, "found_story": True, "should_close": True}

    max_stories = _setting_int("STORY_LIKE_MAX_PER_PROFILE")
    include_highlights = _setting_bool("STORY_LIKE_INCLUDE_HIGHLIGHTS", default=True)
    max_highlights = _setting_int("STORY_LIKE_MAX_HIGHLIGHTS")

    if max_stories <= 0:
        max_stories = None
    if max_highlights <= 0:
        max_highlights = None

    likes_logged = 0

    def on_like_callback(story_url):
        nonlocal likes_logged
        try:
            database.log_story_like_event(username, model_username, story_url, 1)
            likes_logged += 1
        except Exception as e:
            logger.debug(
                f"Failed to log story like event for @{target_username} (model @{model_username}): {e}"
            )

        telegram_bot.stats["story_likes_given"] = int(telegram_bot.stats.get("story_likes_given", 0) or 0) + 1
        if state_obj:
            state_obj["story_likes_today"] = state_obj.get("story_likes_today", 0) + 1
            state_obj["total_story_likes_all_time"] = state_obj.get("total_story_likes_all_time", 0) + 1
            state_obj["last_story_like_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _maybe_send_story_like_milestone(username, model_username)

    result = run_story_liker_script(
        driver,
        target_username,
        max_stories,
        include_highlights,
        max_highlights,
        stop_event,
        on_like=on_like_callback,
    )

    total_liked = int(result.get("total_liked", 0) or 0)
    found_story = bool(result.get("found_story", True))

    if total_liked > likes_logged:
        missing_likes = total_liked - likes_logged
        try:
            database.log_story_like_event(username, model_username, f"batch://{model_username}", missing_likes)
        except Exception as e:
            logger.debug(f"Failed to log batch story like events for @{model_username}: {e}")

    challenge = detect_challenge(driver)
    if challenge == ChallengeType.LOCKED:
        log_and_telegram(f"🚨 Account @{username} was SUSPENDED during the story liking process!")
        telegram_bot.send_lockout_alert(username, "Account suspended during story liking")
        _mark_account_suspended(username, "suspended during story liking")
        return {"likes": total_liked, "found_story": found_story, "should_close": True}

    return {"likes": total_liked, "found_story": found_story, "should_close": False}

