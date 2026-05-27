import os
import sys
import json
import time
import logging
import uuid
import tempfile
import shutil
import random
import math

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException
from core.net_throttle import apply_network_throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cb_bot.bots.cookie")

MAX_DRIVER_RETRIES = 3
DRIVER_RETRY_DELAY = 4

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_driver_lock(lock_path: str, timeout: int = 30):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+")
    start = time.time()
    while True:
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() - start > timeout:
                handle.close()
                raise
            time.sleep(0.2)


def release_driver_lock(handle) -> None:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def parse_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def parse_env_int(name: str, default: int, min_value: int = 1) -> int:
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw is not None else default
    except ValueError:
        value = default
    return max(min_value, value)


def parse_env_messages(name: str) -> list:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in raw.splitlines() if line.strip()]


class CookieViewer:
    def __init__(self, username, cookie_file, proxy=None, headless: bool = False):
        self.username = username
        self.cookie_file = cookie_file
        self.proxy = proxy
        self.driver = None
        self.headless = headless
        self.window_tag = f"CB BOT - {self.username} - {uuid.uuid4().hex[:8]}"
        self.profile_dir = None
        self.msg_enabled = parse_env_bool("CB_MSG_ENABLED", False)
        self.msg_min_seconds = parse_env_int("CB_MSG_MIN_SECONDS", 120, min_value=1)
        self.msg_max_seconds = parse_env_int("CB_MSG_MAX_SECONDS", 300, min_value=1)
        self.msg_texts = parse_env_messages("CB_MSGS_JSON")
        self.last_message = ""
        self.tile_index = parse_env_int("CB_TILE_INDEX", 0, min_value=0)
        self.tile_total = parse_env_int("CB_TILE_TOTAL", 1, min_value=1)
        self.tile_cols = parse_env_int("CB_TILE_COLS", 4, min_value=1)

        if self.msg_min_seconds > self.msg_max_seconds:
            self.msg_min_seconds, self.msg_max_seconds = self.msg_max_seconds, self.msg_min_seconds
        if not self.msg_texts:
            self.msg_enabled = False

    def create_driver(self):
        options = uc.ChromeOptions()
        if not self.profile_dir:
            # Use a unique OS temp profile per browser instance to avoid profile locks.
            self.profile_dir = tempfile.mkdtemp(prefix="cbot_profile_")
        options.add_argument(f"--user-data-dir={self.profile_dir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--remote-debugging-port=0")
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--start-maximized")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        if self.proxy:
            logger.info(f"Using proxy: {self.proxy}")
            options.add_argument(f"--proxy-server={self.proxy}")

        for attempt in range(1, MAX_DRIVER_RETRIES + 1):
            lock_handle = None
            try:
                lock_handle = acquire_driver_lock(os.path.join("data", "temp", "uc.lock"))
                self.driver = uc.Chrome(options=options)
                try:
                    self.driver.execute_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                    )
                except Exception:
                    pass
                apply_network_throttle(self.driver)
                try:
                    if not self.headless:
                        self.driver.maximize_window()
                        self.driver.switch_to.window(self.driver.current_window_handle)
                        self.driver.execute_script("window.focus();")
                except Exception:
                    pass
                return self.driver
            except Exception as e:
                logger.warning(f"Chrome launch failed (attempt {attempt}/{MAX_DRIVER_RETRIES}): {e}")
                if self.driver:
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    self.driver = None
                if attempt < MAX_DRIVER_RETRIES:
                    time.sleep(DRIVER_RETRY_DELAY)
            finally:
                if lock_handle:
                    release_driver_lock(lock_handle)

        raise RuntimeError("Failed to launch Chrome after retries")

    def handle_entrance_terms(self, timeout: int = 8) -> bool:
        """Click 'I AGREE' on entrance terms dialog if present."""
        selectors = [
            (By.ID, "close_entrance_terms"),
            (By.CSS_SELECTOR, "#close_entrance_terms"),
            (By.CSS_SELECTOR, "button#close_entrance_terms"),
            (By.XPATH, "//button[contains(., 'I AGREE') or contains(., 'I Agree')]") ,
            (By.XPATH, "//a[contains(., 'I AGREE') or contains(., 'I Agree')]") ,
            (By.XPATH, "//input[@type='button' and (contains(@value,'I AGREE') or contains(@value,'I Agree'))]")
        ]

        end_time = time.time() + timeout
        while time.time() < end_time:
            for by, sel in selectors:
                try:
                    button = WebDriverWait(self.driver, 1).until(
                        EC.element_to_be_clickable((by, sel))
                    )
                    if button.is_displayed():
                        logger.info("Entrance terms dialog detected. Clicking 'I AGREE'...")
                        try:
                            self.driver.execute_script("arguments[0].click();", button)
                        except Exception:
                            button.click()
                        time.sleep(1)
                        return True
                except (TimeoutException, NoSuchElementException):
                    continue
                except Exception as e:
                    logger.debug(f"Entrance terms click error: {e}")
            time.sleep(0.3)

        logger.debug("No entrance terms dialog found.")
        return False

    def wait_for_page_ready(self, timeout: int = 8) -> None:
        """Wait for the page body to exist; returns immediately if already loaded."""
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass

    def set_window_title(self) -> None:
        """Stamp a unique window title so the OS window can be found."""
        if self.headless:
            return
        try:
            self.driver.execute_script(
                "document.title = arguments[0];"
                "setInterval(() => { document.title = arguments[0]; }, 1000);",
                self.window_tag,
            )
        except Exception:
            pass

    def find_window_handle(self):
        if os.name != "nt" or self.headless:
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return None

        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible

        target_hwnd = None

        def enum_proc(hwnd, _lparam):
            nonlocal target_hwnd
            if not IsWindowVisible(hwnd):
                return True
            length = GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if self.window_tag in title:
                target_hwnd = hwnd
                return False
            return True

        EnumWindows(EnumWindowsProc(enum_proc), 0)
        return target_hwnd

    def tile_window(self) -> bool:
        if os.name != "nt" or self.headless:
            return False
        hwnd = self.find_window_handle()
        if not hwnd:
            return False
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False

        user32 = ctypes.windll.user32
        SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            rect = wintypes.RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

        work_width = max(1, rect.right - rect.left)
        work_height = max(1, rect.bottom - rect.top)
        total = max(1, self.tile_total)

        min_tile_width = 240
        min_tile_height = 360
        max_cols_by_width = max(1, work_width // min_tile_width)

        cols = max(1, self.tile_cols)
        cols = min(cols, total)
        cols = min(cols, max_cols_by_width)
        rows = max(1, math.ceil(total / cols))
        max_rows_by_height = max(1, work_height // min_tile_height)
        rows = min(rows, max_rows_by_height)

        slots = max(1, cols * rows)
        index = self.tile_index % slots
        col = index % cols
        row = index // cols

        base_width = max(1, work_width // cols)
        base_height = max(1, work_height // rows)
        width = base_width if col < cols - 1 else work_width - (base_width * (cols - 1))
        height = base_height if row < rows - 1 else work_height - (base_height * (rows - 1))
        x = rect.left + (col * base_width)
        y = rect.top + (row * base_height)

        SW_RESTORE = 9
        SWP_SHOWWINDOW = 0x0040
        SWP_NOZORDER = 0x0004
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_SHOWWINDOW)
        return True

    def tile_window_with_retry(self, attempts: int = 5, delay: float = 0.3) -> None:
        for _ in range(max(1, attempts)):
            if self.tile_window():
                return
            time.sleep(delay)

    def bring_window_to_front(self) -> None:
        """Bring the current Chrome window to the foreground (Windows only)."""
        if os.name != "nt" or self.headless:
            return
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return

        user32 = ctypes.windll.user32
        ShowWindow = user32.ShowWindow
        SetForegroundWindow = user32.SetForegroundWindow
        SetWindowPos = user32.SetWindowPos
        GetForegroundWindow = user32.GetForegroundWindow
        GetWindowThreadProcessId = user32.GetWindowThreadProcessId
        AttachThreadInput = user32.AttachThreadInput
        BringWindowToTop = user32.BringWindowToTop
        SetFocus = user32.SetFocus

        SW_RESTORE = 9
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040

        target_hwnd = self.find_window_handle()
        if target_hwnd:
            ShowWindow(target_hwnd, SW_RESTORE)
            foreground = GetForegroundWindow()
            fg_tid = GetWindowThreadProcessId(foreground, None)
            target_tid = GetWindowThreadProcessId(target_hwnd, None)
            if fg_tid != target_tid:
                AttachThreadInput(fg_tid, target_tid, True)
            SetWindowPos(target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            SetWindowPos(target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            SetForegroundWindow(target_hwnd)
            BringWindowToTop(target_hwnd)
            SetFocus(target_hwnd)
            if fg_tid != target_tid:
                AttachThreadInput(fg_tid, target_tid, False)

    def focus_window_with_retry(self, attempts: int = 3, delay: float = 0.7) -> None:
        if self.headless:
            return
        for _ in range(max(1, attempts)):
            self.bring_window_to_front()
            time.sleep(delay)

    def schedule_next_message(self) -> float:
        return time.time() + random.uniform(self.msg_min_seconds, self.msg_max_seconds)

    def park_until_stop(self, reason: str) -> None:
        logger.warning(f"{reason} Keeping browser open until manual stop.")
        while True:
            time.sleep(60)

    def clear_chat_input(self, element) -> None:
        try:
            element.clear()
            return
        except Exception:
            pass
        try:
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(Keys.BACKSPACE)
        except Exception:
            pass

    def type_like_human(self, element, text: str) -> None:
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.03, 0.12))

    def find_chat_input(self):
        selectors = [
            (By.CSS_SELECTOR, "textarea#chat-input"),
            (By.CSS_SELECTOR, "textarea[name='message']"),
            (By.CSS_SELECTOR, "textarea[placeholder*='message']"),
            (By.CSS_SELECTOR, "div[data-testid='chat-input']"),
            (By.CSS_SELECTOR, "form.chat-input-form div[contenteditable='true']"),
            (By.CSS_SELECTOR, "div[contenteditable='true'][data-testid='chat-input']"),
            (By.CSS_SELECTOR, "div.chat-input-field[contenteditable='true']"),
            (By.CSS_SELECTOR, "div.inputFieldChatPlaceholder[contenteditable='true']"),
            (By.CSS_SELECTOR, "div.theatermodeInputFieldChat[contenteditable='true']"),
            (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder*='message']"),
            (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
            (By.CSS_SELECTOR, "div[contenteditable='true']")
        ]

        def find_visible_element(by, sel):
            try:
                elements = self.driver.find_elements(by, sel)
            except Exception:
                return None
            for elem in elements:
                try:
                    if elem.is_displayed():
                        return elem
                except Exception:
                    continue
            return None

        def try_find(timeout: float = 6.0):
            end_time = time.time() + timeout
            while time.time() < end_time:
                for by, sel in selectors:
                    elem = find_visible_element(by, sel)
                    if elem:
                        return elem
                time.sleep(0.2)
            return None

        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass

        element = try_find()
        if element:
            return element, False

        try:
            frames = self.driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            frames = []

        for frame in frames:
            found = False
            try:
                self.driver.switch_to.frame(frame)
                element = try_find()
                if element:
                    found = True
                    return element, True
            except Exception:
                continue
            finally:
                if not found:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception:
                        pass

        return None, False

    def send_chat_message(self, message: str) -> bool:
        try:
            element, in_frame = self.find_chat_input()
            if not element:
                logger.warning("Chat input not found; message skipped.")
                return False

            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    element
                )
            except Exception:
                pass

            try:
                element.click()
            except Exception:
                pass

            try:
                self.driver.execute_script("arguments[0].focus();", element)
            except Exception:
                pass

            self.clear_chat_input(element)
            time.sleep(random.uniform(0.2, 0.6))
            self.type_like_human(element, message)
            time.sleep(random.uniform(0.2, 0.5))
            element.send_keys(Keys.ENTER)

            if in_frame:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass

            logger.info(f"MESSAGE_SENT: {message}")
            return True
        except (StaleElementReferenceException, WebDriverException) as exc:
            logger.warning(f"Message send failed: {exc}")
            return False

    def cleanup_profile_dir(self) -> None:
        if self.profile_dir and os.path.isdir(self.profile_dir):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        self.profile_dir = None

    def wait_for_target_room(self, timeout: int = 120) -> bool:
        """Wait until the target room URL is reached (and content begins to load)."""
        end_time = time.time() + timeout
        expected_prefix = f"https://chaturbate.com/{self.username}"
        while time.time() < end_time:
            try:
                current = self.driver.current_url or ""
                if current.startswith(expected_prefix):
                    # Room container is a strong signal, but URL match is enough to proceed.
                    if self.driver.find_elements(By.CLASS_NAME, "room-video-container"):
                        return True
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def run(self):
        logger.info(f"Starting Cookie Viewer for {self.username}...")
        driver = None

        try:
            # --- Load cookies from file ---
            with open(self.cookie_file, 'r') as f:
                cookies = json.load(f)

            if isinstance(cookies, str):
                cookies = json.loads(cookies)
            if isinstance(cookies, dict) and "cookies" in cookies:
                cookies = cookies["cookies"]
            if not isinstance(cookies, list):
                logger.error("Cookie file must contain a JSON list of cookies.")
                return

            logger.info(f"Loaded {len(cookies)} cookies from file.")

            # --- Step 1: Launch browser ---
            logger.info("Launching browser...")
            driver = self.create_driver()
            self.set_window_title()
            self.tile_window_with_retry(attempts=5, delay=0.3)
            self.focus_window_with_retry(attempts=2, delay=0.4)

            # --- Step 2: Navigate to site first (required before adding cookies) ---
            logger.info("Navigating to https://chaturbate.com ...")
            driver.get("https://chaturbate.com")
            self.set_window_title()
            self.tile_window_with_retry(attempts=4, delay=0.3)
            self.focus_window_with_retry(attempts=2, delay=0.4)
            self.wait_for_page_ready(timeout=8)
            self.handle_entrance_terms()  # dismiss popup if it appears on homepage

            # --- Step 3: Delete all existing cookies ---
            logger.info("Clearing existing cookies...")
            driver.delete_all_cookies()

            # --- Step 4: Inject cookies ---
            logger.info(f"Injecting {len(cookies)} cookies...")
            injected_count = 0
            failed_count = 0

            for cookie in cookies:
                name = cookie.get('name', '?')

                # Map expirationDate -> expiry (Chrome extension export format)
                if 'expirationDate' in cookie:
                    cookie['expiry'] = int(cookie.pop('expirationDate'))

                # Strip keys that Selenium/ChromeDriver rejects
                for key in ['hostOnly', 'session', 'storeId', 'id', 'sameSite',
                             'firstPartyDomain', 'size', 'sourcePort', 'sourceScheme']:
                    cookie.pop(key, None)

                # Ensure domain is set
                if 'domain' not in cookie or not cookie['domain']:
                    cookie['domain'] = '.chaturbate.com'

                try:
                    driver.add_cookie(cookie)
                    logger.info(f"  [+] Injected: {name}")
                    injected_count += 1
                except Exception as e:
                    logger.warning(f"  [-] Failed:   {name} -> {e}")
                    failed_count += 1

            logger.info(f"Cookie injection done: {injected_count} OK, {failed_count} failed.")

            # --- Step 5: Refresh to apply cookies ---
            logger.info("Refreshing page to apply cookies...")
            driver.refresh()
            self.wait_for_page_ready(timeout=8)
            self.handle_entrance_terms()  # dismiss popup after refresh

            # --- Step 6: Navigate directly to target model room ---
            logger.info(f"Navigating to target room: https://chaturbate.com/{self.username}/")
            driver.get(f"https://chaturbate.com/{self.username}/")
            self.wait_for_page_ready(timeout=2)

            # Handle entrance terms if shown
            self.handle_entrance_terms()

            # Fast path: signal readiness right after navigation.
            self.set_window_title()
            self.tile_window_with_retry(attempts=4, delay=0.3)
            self.focus_window_with_retry(attempts=3, delay=0.7)
            logger.info("READY: target_loaded")

            # --- Step 8: Stay active (heartbeat loop) ---
            logger.info(f"Bot is now LIVE in {self.username}'s room. Staying active...")
            next_message_at = None
            if self.msg_enabled and self.msg_texts:
                next_message_at = self.schedule_next_message()
                logger.info(
                    f"Auto messages enabled: {len(self.msg_texts)} templates, interval {self.msg_min_seconds}-{self.msg_max_seconds}s"
                )
            else:
                logger.info("Auto messages disabled or no templates configured.")

            last_heartbeat = time.time()
            while True:
                time.sleep(5)
                now = time.time()
                if now - last_heartbeat >= 60:
                    try:
                        driver.find_element(By.TAG_NAME, "body")
                        logger.info(f"Heartbeat OK — watching {self.username}")
                    except Exception:
                        self.park_until_stop("Lost connection to browser.")
                    last_heartbeat = now

                if next_message_at and now >= next_message_at:
                    message = random.choice(self.msg_texts) if self.msg_texts else ""
                    if message and self.last_message and len(self.msg_texts) > 1 and message == self.last_message:
                        message = random.choice([m for m in self.msg_texts if m != self.last_message])
                    if message:
                        self.send_chat_message(message)
                        self.last_message = message
                    next_message_at = self.schedule_next_message()

        except Exception as e:
            logger.error(f"Cookie Viewer error: {e}", exc_info=True)
            if driver:
                self.park_until_stop("Unhandled error.")
        finally:
            close_on_exit = parse_env_bool("CB_CLOSE_ON_EXIT", False)
            if driver and close_on_exit:
                try:
                    driver.quit()
                except Exception:
                    pass
                self.cleanup_profile_dir()
            elif not driver:
                self.cleanup_profile_dir()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python cookieviewer.py <username> <cookie_file_path> [--proxy <proxy>]")
        sys.exit(1)

    username = sys.argv[1]
    cookie_path = sys.argv[2]
    proxy = None
    headless = False
    args = sys.argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--proxy" and i + 1 < len(args):
            proxy = args[i + 1]
            i += 2
            continue
        if args[i] == "--headless":
            headless = True
            i += 1
            continue
        i += 1

    bot = CookieViewer(username, cookie_path, proxy, headless=headless)
    bot.run()
