import os
import sys

# Ensure we can import from root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import logging
import random
from core.solver import CloudflareSolver
from core.user_gen import generate_user
from core.temp_mail import generate_email, get_inbox
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger("cb_bot.bots.reg")

class RegistrationBot:
    def __init__(self, proxy=None):
        self.proxy = proxy
        self.solver = CloudflareSolver(proxy=proxy)
        self.target_url = "https://chaturbate.com/accounts/register/"

    def run(self):
        logger.info("Starting Registration Bot...")
        driver = self.solver.create_driver()
        
        try:
            # 1. Navigate and bypass Cloudflare
            success = self.solver.bypass_cloudflare(self.target_url)
            if not success:
                logger.error("Could not reach registration page.")
                return

            user = generate_user()
            user['email'] = generate_email()
            if not user['email']:
                logger.error("Could not generate temporary email.")
                return
                
            logger.info(f"Registering account: {user['username']} with {user['email']}")

            # 2. Fill Form
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "husername")))
            
            driver.find_element(By.ID, "husername").send_keys(user['username'])
            time.sleep(random.uniform(0.5, 1.2))
            driver.find_element(By.ID, "hpassword").send_keys(user['password'])
            time.sleep(random.uniform(0.5, 1.2))
            driver.find_element(By.ID, "id_email").send_keys(user['email'])
            
            # Birthday and Gender
            driver.find_element(By.ID, "id_birthday_month").send_keys(user['month'])
            driver.find_element(By.ID, "id_birthday_day").send_keys(user['day'])
            driver.find_element(By.ID, "id_birthday_year").send_keys(user['year'])
            driver.find_element(By.ID, "id_gender").send_keys(user['gender'])

            # Agreements
            driver.find_element(By.ID, "id_terms").click()
            driver.find_element(By.ID, "id_privacy_policy").click()

            logger.info("Form filled. Manual captcha solve or automation here.")
            
            # 3. Handle Captcha (Site uses ReCaptcha V2 or Turnstile)
            # The user's solver already handles Turnstile if it appears.
            # If it's ReCaptcha, we'd use RecaptchaPlugin equivalent or 2Captcha API.
            
            time.sleep(5) # Delay for visual verification in this demo
            
            # 4. Submit
            driver.find_element(By.ID, "formsubmit").click()
            
            # 5. Verify and save
            # (Wait for redirect to profile or confirmation)
            time.sleep(10)
            logger.info("Registration attempt complete.")
            
        except Exception as e:
            logger.error(f"Registration error: {e}")
        finally:
            driver.quit()

if __name__ == "__main__":
    bot = RegistrationBot()
    bot.run()
