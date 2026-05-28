import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
import time
import subprocess
from core.chrome import detect_installed_chrome_major_version

# Proxy configuration
proxy_url = "spq6f2mvt5:PR9u2usHeo=di7dJ3r@isp.decodo.com:10001"

def setup_with_undetected_chrome():
    """Setup using undetected-chromedriver with version detection"""
    
    # Get Chrome version
    chrome_version = detect_installed_chrome_major_version()
    if chrome_version:
        print(f"📌 Detected Chrome version: {chrome_version}")
    else:
        print("⚠️ Could not detect Chrome version, will auto-detect")
    
    chrome_options = uc.ChromeOptions()
    
    # Add proxy
    chrome_options.add_argument(f'--proxy-server=http://{proxy_url}')
    
    # Stealth options
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--start-maximized')
    chrome_options.add_argument('--disable-automation')
    chrome_options.add_argument('--disable-web-security')
    chrome_options.add_argument('--disable-features=VizDisplayCompositor')
    chrome_options.add_argument('--disable-features=IsolateOrigins,site-per-process')
    
    print("Launching browser with undetected-chromedriver...")
    
    # Let undetected-chromedriver auto-detect Chrome version
    # Set version_main=None to auto-detect
    driver = uc.Chrome(
        options=chrome_options, 
        version_main=None,  # Auto-detect
        use_subprocess=True  # Better for version handling
    )
    
    # Execute CDP commands for stealth
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': '''
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        '''
    })
    
    return driver

def main():
    driver = None
    try:
        print("="*60)
        print("🔄 Setting up browser with proxy for whoer.net...")
        print("="*60)
        
        driver = setup_with_undetected_chrome()
        
        # Test proxy
        print("\n🔍 Testing proxy connection...")
        driver.get('https://httpbin.org/ip')
        time.sleep(3)
        print(f"📡 Proxy response: {driver.find_element(By.TAG_NAME, 'body').text}")
        
        # Navigate to whoer.net
        print("\n🌐 Navigating to whoer.net...")
        driver.get('https://whoer.net')
        
        print("\n✓ whoer.net loaded!")
        print("="*60)
        print("📊 Check your anonymity score on whoer.net")
        print("⏰ Browser will stay open for 2 minutes")
        print("💡 Press Ctrl+C to close early")
        print("="*60)
        
        # Take screenshot
        time.sleep(5)
        driver.save_screenshot("whoer_screenshot.png")
        print("📸 Screenshot saved as whoer_screenshot.png")
        
        # Keep browser open
        time.sleep(120)
        
    except KeyboardInterrupt:
        print("\n\nClosing browser...")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\n💡 Troubleshooting suggestions:")
        print("1. Update Chrome browser to latest version")
        print("2. Try running: pip install --upgrade undetected-chromedriver")
        print("3. Close all Chrome windows and try again")
    finally:
        if driver:
            driver.quit()
            print("✓ Browser closed")

if __name__ == "__main__":
    # Install required packages
    try:
        import undetected_chromedriver
        print("✅ undetected-chromedriver found")
    except ImportError:
        print("📦 Installing undetected-chromedriver...")
        subprocess.check_call(['pip', 'install', 'undetected-chromedriver'])
        print("✅ Installation complete. Please run the script again")
        exit()
    
    # Check for selenium
    try:
        import selenium
        print("✅ selenium found")
    except ImportError:
        print("📦 Installing selenium...")
        subprocess.check_call(['pip', 'install', 'selenium'])
    
    main()