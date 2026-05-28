import glob
import os
import re
import shutil
import subprocess
import sys
import uuid
from typing import Optional


def parse_version_major(version_text: str) -> Optional[int]:
    if not version_text:
        return None
    text = str(version_text).strip()
    if not text:
        return None

    match = re.search(r"(\d+)\.", text)
    if not match:
        match = re.search(r"(\d+)", text)
    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None


def _run_version_command(command: list) -> Optional[int]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    output = (result.stdout or result.stderr or "").strip()
    return parse_version_major(output)


def _chrome_binary_candidates() -> list:
    candidates = []

    env_bin = (
        os.getenv("CHROME_BINARY", "").strip()
        or os.getenv("CHROME_BIN", "").strip()
        or os.getenv("CHROME_PATH", "").strip()
    )
    if env_bin:
        candidates.append(env_bin)

    if os.name == "nt":
        for base in (
            os.getenv("PROGRAMFILES", ""),
            os.getenv("PROGRAMFILES(X86)", ""),
            os.getenv("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            candidates.append(os.path.join(base, "Chromium", "Application", "chrome.exe"))

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(name)
        if path:
            candidates.append(path)

    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])

    return candidates


def resolve_chrome_binary() -> str:
    seen = set()
    for candidate in _chrome_binary_candidates():
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    return ""


def detect_installed_chrome_major_version() -> Optional[int]:
    env_main = (
        os.getenv("CHROME_VERSION_MAIN", "").strip()
        or os.getenv("UC_VERSION_MAIN", "").strip()
    )
    major = parse_version_major(env_main)
    if major:
        return major

    if os.name == "nt":
        try:
            import winreg
        except Exception:
            winreg = None
        if winreg:
            reg_paths = [
                (winreg.HKEY_CURRENT_USER, r"Software\\Google\\Chrome\\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\\Google\\Chrome\\BLBeacon"),
                (winreg.HKEY_CURRENT_USER, r"Software\\Chromium\\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\\Chromium\\BLBeacon"),
            ]
            for root, path in reg_paths:
                try:
                    with winreg.OpenKey(root, path) as key:
                        version_text, _ = winreg.QueryValueEx(key, "version")
                    major = parse_version_major(version_text)
                    if major:
                        return major
                except Exception:
                    continue

    seen = set()
    for candidate in _chrome_binary_candidates():
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if not os.path.isfile(candidate):
            continue
        major = _run_version_command([candidate, "--version"])
        if major:
            return major

    return None


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


def lock_chrome_version(env_path: str):
    existing = os.getenv("CHROME_VERSION_MAIN", "").strip()
    if existing and existing.lower() not in ("auto", "latest", "local"):
        return parse_version_major(existing) or existing

    major = detect_installed_chrome_major_version()
    if not major:
        return None

    os.environ["CHROME_VERSION_MAIN"] = str(major)
    if existing and existing.lower() in ("auto", "latest", "local"):
        return major

    updated = update_env_file(env_path, "CHROME_VERSION_MAIN", str(major))
    if not updated:
        return major
    return major


def get_chromedriver_major_version(driver_path: str) -> Optional[int]:
    return _run_version_command([driver_path, "--version"])


def resolve_chromedriver_path(required_major: Optional[int]) -> str:
    candidates = []

    env_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if env_path:
        candidates.append(env_path)

    found = shutil.which("chromedriver")
    if found:
        candidates.append(found)

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            pattern = os.path.join(
                local_app_data,
                "Microsoft",
                "WinGet",
                "Packages",
                "*ChromeDriver*",
                "chromedriver-win64",
                "chromedriver.exe",
            )
            candidates.extend(glob.glob(pattern))
    else:
        candidates.extend([
            "/usr/local/bin/chromedriver",
            "/usr/bin/chromedriver",
            "/opt/homebrew/bin/chromedriver",
            os.path.expanduser("~/.local/bin/chromedriver"),
            "/snap/bin/chromedriver",
        ])

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if not os.path.isfile(candidate):
            continue
        if not required_major:
            return candidate
        driver_major = get_chromedriver_major_version(candidate)
        if driver_major == required_major:
            return candidate

    return ""


def prepare_chromedriver_copy(required_major=None) -> str:
    base_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if not base_path or not os.path.isfile(base_path):
        return ""

    allow_mismatch = os.getenv("CHROMEDRIVER_ALLOW_MISMATCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )

    driver_major = get_chromedriver_major_version(base_path)
    if required_major and driver_major and driver_major != required_major and not allow_mismatch:
        return ""

    dest_dir = os.path.abspath(os.path.join("data", "temp", "uc_drivers", uuid.uuid4().hex))
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(base_path))
    try:
        shutil.copy2(base_path, dest_path)
        return dest_path
    except Exception:
        return base_path
