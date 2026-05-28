import os
import sys

# Ensure we can import from root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import time
import logging
from core.solver import CloudflareSolver
from selenium.webdriver.common.by import By

logger = logging.getLogger("cb_bot.bots.real")

class RealAccountBot:
    def __init__(self, username, accounts_path="data/test-accounts", proxy=None):
        self.username = username
        self.accounts_path = accounts_path
        self.proxy = proxy
        self.solver = CloudflareSolver(proxy=proxy)

    def run(self):
        logger.info(f"Starting Real Account Bot for {self.username}...")
        
        if not os.path.exists(self.accounts_path):
            logger.error(f"Accounts path {self.accounts_path} not found.")
            return

        account_files = [f for f in os.listdir(self.accounts_path) if f.endswith('.json')]
        
        for account_file in account_files:
            driver = None
            try:
                with open(os.path.join(self.accounts_path, account_file), 'r') as f:
                    account_data = json.load(f)
                
                logger.info(f"Logging in with account: {account_data['user']['username']}")
                
                # 1. Use solver to bypass Cloudflare and set context
                logger.info("Bypassing Cloudflare...")
                self.solver.bypass_cloudflare("https://chaturbate.com/")
                driver = self.solver.driver
                
                # Delete any existing cookies first
                driver.delete_all_cookies()
                time.sleep(1)
                
                # 2. Inject Cookies
                logger.info(f"Injecting {len(account_data['cookies'])} cookies...")
                for cookie in account_data['cookies']:
                    # Clean cookie for Selenium
                    if 'expirationDate' in cookie:
                        cookie['expiry'] = int(cookie.pop('expirationDate'))
                    
                    # Remove non-selenium fields
                    for key in ['hostOnly', 'session', 'storeId', 'id', 'sameSite', 'firstPartyDomain']:
                        cookie.pop(key, None)
                    
                    try:
                        driver.add_cookie(cookie)
                    except Exception as e:
                        logger.debug(f"Skip cookie {cookie.get('name')}: {e}")

                # 3. Refresh and Navigate to the room
                driver.refresh()
                time.sleep(2)
                driver.get(f"https://chaturbate.com/{self.username}")
                
                # 4. Wait and Interact
                # (Same logic as anonymous, but with real account context)
                time.sleep(60) # Stay for 60 seconds as per original JS logic
                
            except Exception as e:
                logger.error(f"Error processing account {account_file}: {e}")
            finally:
                if driver:
                    driver.quit()

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python realaccount.py <model_username> [accounts_path] [proxy]")
        sys.exit(1)
        
    model = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "data/test-accounts"
    proxy = sys.argv[3] if len(sys.argv) > 3 else None
    
    bot = RealAccountBot(model, path, proxy)
    bot.run()
