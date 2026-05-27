import time
import random
import logging
from typing import Optional, Union

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from core.net_throttle import apply_network_throttle

logger = logging.getLogger("cb_bot.core.solver")

class CloudflareSolver:
    """
    Base class for Selenium-based automation with Cloudflare bypass capabilities.
    """

    def __init__(
        self,
        headless: bool = False,
        timeout: int = 30,
        retries: int = 20,
        proxy: Optional[str] = None,
    ):
        self.headless = headless
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.driver: Optional[uc.Chrome] = None

    def get_proxy_extension(self, proxy_str: str) -> Optional[str]:
        """Create a temporary Chrome extension folder to handle proxy authentication."""
        import os
        import uuid
        from urllib.parse import urlparse

        # Ensure scheme is present for parsing
        if not proxy_str.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
            proxy_url = f"http://{proxy_str}"
        else:
            proxy_url = proxy_str

        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        user = parsed.username
        password = parsed.password
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https", "socks5", "socks4"):
            scheme = "http"

        if not host or not port:
            return None

        if not user or not password:
            return None # No auth needed

        manifest_json = """
        {
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Chrome Proxy",
            "permissions": [
                "proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>", "webRequest", "webRequestBlocking"
            ],
            "background": { "scripts": ["background.js"] },
            "minimum_chrome_version":"22.0.0"
        }
        """

        background_js = f"""
        var config = {{
                mode: "fixed_servers",
                rules: {{
                singleProxy: {{
                    scheme: "{scheme}",
                    host: "{host}",
                    port: parseInt({port})
                }},
                bypassList: ["localhost"]
                }}
            }};

        chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

        chrome.webRequest.onAuthRequired.addListener(
            function(details) {{
                return {{
                    authCredentials: {{
                        username: "{user}",
                        password: "{password}"
                    }}
                }};
            }},
            {{urls: ["<all_urls>"]}},
            ["blocking"]
        );
        """

        ext_dir = os.path.abspath(
            os.path.join("data", "proxy_ext", f"{host}_{port}_{uuid.uuid4().hex}")
        )
        os.makedirs(ext_dir, exist_ok=True)
        with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(manifest_json)
        with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
            f.write(background_js)
        
        return ext_dir

    def create_driver(self) -> uc.Chrome:
        """Create an undetected Chrome instance."""
        options = uc.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        if self.proxy:
            plugin_path = self.get_proxy_extension(self.proxy)
            if plugin_path:
                options.add_argument(f"--load-extension={plugin_path}")
            else:
                options.add_argument(f"--proxy-server={self.proxy}")

        self.driver = uc.Chrome(options=options)
        try:
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception:
            pass
        apply_network_throttle(self.driver)
        return self.driver

    def human_click(self, element) -> None:
        """Perform a human-like click with random offset."""
        try:
            # Random offset within the element
            size = element.size
            ox = random.randint(-size['width'] // 4, size['width'] // 4)
            oy = random.randint(-size['height'] // 4, size['height'] // 4)

            action = ActionChains(self.driver)
            action.move_to_element_with_offset(element, ox, oy)
            action.click()
            action.perform()
            action.reset_actions()
        except Exception as e:
            logger.warning(f"ActionChains click failed, falling back to direct click: {e}")
            element.click()

    def find_challenge_iframe(self) -> bool:
        """Find and solve Cloudflare Turnstile iframe."""
        try:
            iframe = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'iframe[src*="challenges.cloudflare.com"]')
                )
            )
            self.driver.switch_to.frame(iframe)

            checkbox = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#challenge-stage input[type=checkbox]"))
            )
            
            time.sleep(random.uniform(1.0, 2.5))
            self.human_click(checkbox)
            time.sleep(random.uniform(1.0, 2.0))

            self.driver.switch_to.default_content()
            logger.info("Turnstile checkbox clicked successfully.")
            return True

        except Exception as e:
            logger.debug(f"Challenge iframe not found or error: {e}")
            return False

    def wait_for_clearance(self) -> bool:
        """Wait until the page is cleared of Cloudflare."""
        for _ in range(self.retries):
            # Check if challenge element is gone or if we have the clearance cookie
            cookies = self.driver.get_cookies()
            if any(c["name"] == "cf_clearance" for c in cookies):
                logger.info("Cloudflare clearance obtained via cookie.")
                return True
            
            # Or check if a known element from the site is visible
            try:
                # Chaturbate specific element
                if self.driver.find_elements(By.CLASS_NAME, "room-video-container"):
                    logger.info("Room content detected, Cloudflare bypassed.")
                    return True
            except:
                pass
                
            time.sleep(2)
        return False

    def handle_entrance_terms(self) -> bool:
        """Click 'I AGREE' on the entrance terms dialog if present."""
        try:
            # Check if the dialog button is present (id from user snippet)
            # We use a short wait to avoid blocking if not present
            wait = WebDriverWait(self.driver, 5)
            button = wait.until(EC.element_to_be_clickable((By.ID, "close_entrance_terms")))
            
            if button.is_displayed():
                logger.info("Entrance terms dialog detected. Clicking 'I AGREE'...")
                time.sleep(1) # Small delay for realism
                self.human_click(button)
                time.sleep(2) # Wait for dialog to close
                return True
        except (TimeoutException, NoSuchElementException):
            logger.debug("Entrance terms dialog not found.")
        except Exception as e:
            logger.debug(f"Error handling entrance terms: {e}")
        return False

    def bypass_cloudflare(self, url: str) -> bool:
        """Navigate and attempt to bypass Cloudflare."""
        if not self.driver:
            self.create_driver()
            
        logger.info(f"Navigating to {url}")
        self.driver.get(url)
        
        for _ in range(3): # Try up to 3 times to find and click the iframe
            if self.find_challenge_iframe():
                break
            time.sleep(3)
            
        # After CF bypass, check for entrance terms
        self.handle_entrance_terms()
        
        return self.wait_for_clearance()
