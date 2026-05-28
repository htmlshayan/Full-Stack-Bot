from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
import time

def open_multiple_browsers(num_browsers=5, width=300, height=300):
    drivers = []
    
    for i in range(num_browsers):
        chrome_options = Options()
        chrome_options.add_argument(f"--window-size={width},{height}")
        
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        driver.get("https://www.google.com")
        drivers.append(driver)
        print(f"Browser {i+1}: {width}x{height}")
        time.sleep(0.5)
    
    return drivers

# Open 5 browsers at 300x300
browsers = open_multiple_browsers(5, 300, 300)

# Keep browsers open
input("\nPress Enter to close all browsers...")

# Close all
for driver in browsers:
    driver.quit()