import logging

logger = logging.getLogger("cb_bot.core.net")

# Hardcoded throttling defaults (edit here to change globally)
DEFAULT_KBPS_DOWN = 500.0
DEFAULT_KBPS_UP = 300.0
DEFAULT_LATENCY_MS = 100
DEFAULT_CONNECTION = "cellular3g"
DEFAULT_OFFLINE = False


def apply_network_throttle(driver) -> bool:
    down_kbps = DEFAULT_KBPS_DOWN
    up_kbps = DEFAULT_KBPS_UP
    latency_ms = DEFAULT_LATENCY_MS
    offline = DEFAULT_OFFLINE
    connection_type = DEFAULT_CONNECTION

    if offline:
        params = {
            "offline": True,
            "latency": max(0, latency_ms),
            "downloadThroughput": 0,
            "uploadThroughput": 0,
        }
    else:
        if down_kbps <= 0 or up_kbps <= 0:
            return False
        down_bps = int(down_kbps * 1024 / 8)
        up_bps = int(up_kbps * 1024 / 8)
        params = {
            "offline": False,
            "latency": max(0, latency_ms),
            "downloadThroughput": down_bps,
            "uploadThroughput": up_bps,
        }
        if connection_type:
            params["connectionType"] = connection_type

    try:
        driver.execute_cdp_cmd("Network.emulateNetworkConditions", params)
        logger.info(
            "Applied network throttle: offline=%s down=%s kbps up=%s kbps latency=%sms",
            params.get("offline"),
            down_kbps,
            up_kbps,
            params.get("latency", 0),
        )
        return True
    except Exception as exc:
        logger.warning("Failed to apply network throttle: %s", exc)
        return False
