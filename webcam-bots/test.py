import time
import random
import logging
from typing import Optional, Union

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from core.net_throttle import apply_network_throttle

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class CloudflareSolverSelenium:
    """
    Solves Cloudflare anti‑bot challenges using undetected_chromedriver.
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

    def _create_driver(self) -> uc.Chrome:
        """Create an undetected Chrome instance with human‑like options."""
        options = uc.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")  # new headless mode
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--window-size=1920,1080")

        # Optional proxy
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")

        driver = uc.Chrome(options=options)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        apply_network_throttle(driver)
        return driver

    def _human_click(self, element) -> None:
        """Move to element with a slight random offset and click."""
        # Get element location and size
        location = element.location
        size = element.size
        # Random offset within the element (center ± a few pixels)
        x = location['x'] + size['width'] // 2 + random.randint(-3, 3)
        y = location['y'] + size['height'] // 2 + random.randint(-3, 3)

        # Use ActionChains to move smoothly
        action = uc.webdriver.ActionChains(self.driver)
        action.move_by_offset(x, y)
        action.click()
        action.perform()
        action.reset_actions()  # clear stored offsets

    def _find_challenge_iframe(self) -> bool:
        """Locate the Cloudflare challenge iframe and click the checkbox inside."""
        try:
            # The iframe is typically loaded from challenges.cloudflare.com
            iframe = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'iframe[src*="challenges.cloudflare.com"]')
                )
            )
            # Switch to the iframe
            self.driver.switch_to.frame(iframe)

            # Wait for the checkbox element inside the iframe
            checkbox = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#challenge-stage input[type=checkbox]"))
            )
            # Simulate human click with slight random delay
            time.sleep(random.uniform(1.5, 3))
            self._human_click(checkbox)
            time.sleep(random.uniform(0.5, 1.5))

            # Switch back to default content
            self.driver.switch_to.default_content()
            logger.info("Clicked Cloudflare challenge checkbox.")
            return True

        except (TimeoutException, NoSuchElementException):
            logger.info("No Cloudflare challenge iframe found or checkbox not clickable.")
            return False
        except Exception as e:
            logger.error(f"Error handling challenge iframe: {e}")
            return False

    def _wait_for_clearance(self) -> Optional[str]:
        """Wait until cf_clearance cookie is set and return its value."""
        for attempt in range(self.retries):
            cookies = self.driver.get_cookies()
            cf_cookie = next((c for c in cookies if c["name"] == "cf_clearance"), None)
            if cf_cookie:
                logger.info(f"cf_clearance cookie obtained: {cf_cookie['value'][:20]}...")
                return cf_cookie["value"]
            time.sleep(1)
        logger.warning("cf_clearance cookie not found after multiple retries.")
        return None

    def _get_turnstile_token(self) -> Optional[str]:
        """Extract Turnstile token from hidden input if present."""
        try:
            # Look for the hidden input field (common name)
            token_input = self.driver.find_element(By.NAME, "cf-turnstile-response")
            token = token_input.get_attribute("value")
            if token and len(token) > 10:
                logger.info(f"Turnstile token obtained: {token[:20]}...")
                return token
        except NoSuchElementException:
            pass
        logger.info("No Turnstile token found.")
        return None

    def solve(self, url: str) -> Optional[Union[str, dict]]:
        """
        Navigate to the given URL, solve any Cloudflare challenge,
        and return the clearance cookie or Turnstile token.
        """
        self.driver = self._create_driver()
        try:
            logger.info(f"Navigating to {url}")
            self.driver.get(url)

            # Wait for initial page to load / challenge to appear
            WebDriverWait(self.driver, self.timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Attempt to solve the challenge (may appear after a redirect)
            solved = False
            for attempt in range(self.retries):
                if self._find_challenge_iframe():
                    # Challenge frame was present and we clicked; wait for clearance
                    time.sleep(5)  # typical challenge resolution time
                    solved = True
                    break
                time.sleep(1)

            # If no challenge frame appeared, the page might have loaded directly
            # We still check for cookies / token
            cf_clearance = self._wait_for_clearance()
            if cf_clearance:
                return {
                    "cf_clearance": cf_clearance,
                    "type": "challenge"
                }

            # Fallback: check for Turnstile token
            turnstile_token = self._get_turnstile_token()
            if turnstile_token:
                return {
                    "token": turnstile_token,
                    "type": "turnstile"
                }

            # If nothing found, return None but keep browser open for debugging
            logger.warning("No Cloudflare clearance cookie or Turnstile token obtained.")
            return None

        finally:
            # Keep the browser open? Usually you'd want to use the page.
            # For this example we close it; adjust as needed.
            if self.driver:
                self.driver.quit()

    def navigate_solved(self, url: str) -> uc.Chrome:
        """
        Returns the live driver after solving Cloudflare (or raising an exception).
        The caller must quit the driver afterwards.
        """
        driver = self._create_driver()
        driver.get(url)
        WebDriverWait(driver, self.timeout).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        for _ in range(self.retries):
            if self._find_challenge_iframe():
                time.sleep(5)
                break
            time.sleep(1)

        cf = self._wait_for_clearance()  # check once more
        if not cf and not self._get_turnstile_token():
            logger.warning("Could not solve Cloudflare automatically; page may still be blocked.")
        return driver


# ---------- Example usage for https://chat.com/jadelove_/ ----------
if __name__ == "__main__":
    TARGET_URL = "https://chaturbate.com/jadelove_/"

    solver = CloudflareSolverSelenium(
        headless=False,   # Set to True for headless mode
        timeout=20,
        retries=15,
    )

    # Option 1: solve and get the cookie/token (driver is closed)
    result = solver.solve(TARGET_URL)
    if result:
        print("Cloudflare solved!")
        print(result)
    else:
        print("Failed to solve Cloudflare.")

    # Option 2: keep the browser open to interact further
    driver = solver.navigate_solved(TARGET_URL)
    # Now you can scrape or interact with the page
    time.sleep(10)  # manually inspect
    driver.quit()