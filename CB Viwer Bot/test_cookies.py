import undetected_chromedriver as uc
import time
from core.net_throttle import apply_network_throttle

# Your cookies - use the fresh ones you provided
cookies = [
    {"name": "__cf_bm", "value": "AOF6dz_36Ke38Q7_Ma8FjebAzpt9MJVIZWROlMBzzdc-1778798274.9550858-1.0.1.1-V3j5nHHkPWgzvbU9w89RXTVJAntsOH6XQ_3YOWyfbS0KcrD_z97vYK8Cq3kCzZT5kbEBnGXTlwAItzYTCHS9Y8V31oWR.4ekOlP4cZtf8Pp7Un9_Ebe8jc1CIzO7ENAq", "domain": ".chaturbate.com", "path": "/"},
    {"name": "sessionid", "value": "tin5k7av9aj38sa47e5tiyxx5kcm9nvk", "domain": ".chaturbate.com", "path": "/"},
    {"name": "__utfpp", "value": "f:trnx5d31912fb8a17318028a96feb6fa72e8:1wNeUs:2rijdbYtFeFzfiW_IWLT4C5jl0JdL2QArtWbl9BP8_Y", "domain": ".chaturbate.com", "path": "/"},
    {"name": "_iidt", "value": "a84zheD8pwQ3IXvq7xx+r8GwvayQDjUZLJ657aHnwY3yCZQT0Fktatbw2Ge6VyJQjqA5TwcjTeHdLMHHPg3DkA/yQ72nCBemK7o6L8FTAlrrmRYT+A==", "domain": ".chaturbate.com", "path": "/"},
    {"name": "affkey", "value": "eJxtykEKgCAQheGrDLOZjYJO2cLbRCJEBDG5E+8eE2ibdh/vfxUFIyAawKRgx4t1wfoZmCOHOHltuWislCgC/XzIABU5tCpv2Tqvse2pc/soYzvHs3SsOb9s2B6JOCO5", "domain": ".chaturbate.com", "path": "/"},
    {"name": "csrftoken", "value": "izWT7GnBVxvJHmWe72RwAIAcY0xoHpGs", "domain": ".chaturbate.com", "path": "/"},
    {"name": "sbr", "value": "sec:sbr71be4769-8404-40ee-baf6-7002efb41923:1wNeUl:eXJFjh5mNUtC2-3dBue3nYVOh72dh4QyH9I-jipDfgI", "domain": ".chaturbate.com", "path": "/"},
]

# Setup and run
options = uc.ChromeOptions()
options.add_argument('--disable-blink-features=AutomationControlled')

driver = uc.Chrome(options=options)
apply_network_throttle(driver)

try:
    # Go to website first
    driver.get("https://chaturbate.com")
    time.sleep(2)
    
    # Delete any existing cookies
    driver.delete_all_cookies()
    
    # Inject your cookies
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
            print(f"✓ Added: {cookie['name']}")
        except Exception as e:
            print(f"✗ Failed: {cookie['name']} - {e}")
    
    # Refresh to apply cookies
    driver.refresh()
    time.sleep(3)
    
    # Check if logged in - go to account page
    driver.get("https://chaturbate.com/account/")
    time.sleep(3)
    
    # Show result
    current_url = driver.current_url
    print(f"\n📍 Current URL: {current_url}")
    
    if "login" in current_url:
        print("\n❌ NOT LOGGED IN - Cookies don't work")
        print("   The cookies might be expired or tied to another IP")
    else:
        print("\n✅ SUCCESS! YOU ARE LOGGED IN!")
        print("   Browser will stay open. Check manually.")
    
    # Keep browser open so you can see
    input("\nPress Enter to close browser...")
    
finally:
    driver.quit()