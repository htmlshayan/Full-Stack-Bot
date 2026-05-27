import os
import sys

# Ensure we can import from root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import json
import asyncio
import logging
import random
import threading
import websockets
import aiohttp
from aiohttp_socks import ProxyConnector
from core.solver import CloudflareSolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cb_bot.bots.anon")

class AnonymousViewer:
    def __init__(self, username, count=50, browsers=1, proxy=None):
        self.username = username
        self.count = int(count)
        self.browser_count = int(browsers)
        self.proxy = proxy
        self.dossier = None
        self.dossier_lock = threading.Lock()

    async def create_viewer(self, dossier):
        try:
            ws_host = dossier['wschat_host'].replace('http', 'ws').replace('https', 'wss')
            random_id = random.randint(100, 999)
            random_path = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=8))
            ws_url = f"{ws_host}/{random_id}/{random_path}/websocket"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Origin": "https://chaturbate.com"
            }
            # Ensure proxy URL is formatted correctly for aiohttp-socks
            proxy_url = self.proxy
            if proxy_url and not proxy_url.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
                proxy_url = f"http://{proxy_url}"
            
            if proxy_url:
                logger.info(f"Viewer attempting connection via proxy: {proxy_url}")
            
            connector = ProxyConnector.from_url(proxy_url) if proxy_url else None
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(ws_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as ws:
                    while True:
                        msg = await ws.receive_str()
                        if not msg: break
                        
                        if msg == 'o':
                            await ws.send_str(json.dumps([json.dumps({
                                "method": "connect",
                                "data": {
                                    "password": dossier['chat_password'],
                                    "room": dossier['broadcaster_username'],
                                    "room_password": dossier['room_pass'],
                                    "user": dossier['chat_username']
                                }
                            })]))
                        elif msg == 'h':
                            await ws.send_str('h') 
                        elif msg.startswith('a'):
                            try:
                                raw_data = json.loads(msg[1:])
                                for frame in raw_data:
                                    data = json.loads(frame)
                                    if data.get('method') == 'onAuthResponse':
                                        await ws.send_str(json.dumps([json.dumps({
                                            "method": "joinRoom",
                                            "data": {"room": dossier['broadcaster_username']}
                                        })]))
                            except Exception as e:
                                logger.debug(f"Error parsing msg: {e}")
        except Exception as e:
            logger.error(f"Viewer connection failed: {e}")

    def browser_worker(self, index):
        """Worker thread to handle a single browser instance."""
        logger.info(f"Browser #{index+1}: Launching...")
        solver = CloudflareSolver(proxy=self.proxy)
        try:
            solver.create_driver()
            success = solver.bypass_cloudflare(f"https://chaturbate.com/{self.username}")
            
            if not success:
                logger.error(f"Browser #{index+1}: Failed to bypass Cloudflare.")
                return
            
            # Click I AGREE if terms appear
            solver.handle_entrance_terms()

            # Extract dossier if not already found
            with self.dossier_lock:
                if not self.dossier:
                    dossier_json = solver.driver.execute_script(
                        "return JSON.stringify(window.initialRoomDossier)"
                    )
                    if dossier_json:
                        try:
                            dossier = json.loads(dossier_json)
                        except Exception as e:
                            logger.debug(f"Browser #{index+1}: Dossier parse error: {e}")
                            dossier = None
                        if dossier:
                            self.dossier = dossier
                            logger.info(f"Browser #{index+1}: Successfully obtained room data.")

            logger.info(f"Browser #{index+1}: Staying active to maintain session.")
            # Keep browser open to maintain the session
            while True:
                time.sleep(60)
        except Exception as e:
            logger.error(f"Browser #{index+1} error: {e}")
        finally:
            if solver.driver:
                solver.driver.quit()

    async def run(self):
        logger.info(f"Starting {self.browser_count} browsers for {self.username}...")
        
        # 1. Start browser threads
        threads = []
        for i in range(self.browser_count):
            t = threading.Thread(target=self.browser_worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)
            await asyncio.sleep(2) # Staggered launch (async)

        # 2. Wait for at least one browser to get the dossier
        logger.info("Waiting for first browser to bypass Cloudflare...")
        while not self.dossier:
            await asyncio.sleep(1)
        
        # 3. Spawn WebSocket viewers
        logger.info(f"Spawning {self.count} WebSocket viewers...")
        tasks = []
        for i in range(self.count):
            tasks.append(self.create_viewer(self.dossier))
            if i % 10 == 0: await asyncio.sleep(0.5)
        
        logger.info(f"All viewers spawned. Browsers and viewers are active.")
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python anonymous.py <username> [count] [browsers] [proxy]")
        sys.exit(1)
        
    username = sys.argv[1]
    count = sys.argv[2] if len(sys.argv) > 2 else 50
    browsers = sys.argv[3] if len(sys.argv) > 3 else 1
    proxy = sys.argv[4] if len(sys.argv) > 4 else None
    
    bot = AnonymousViewer(username, count, browsers, proxy)
    asyncio.run(bot.run())
