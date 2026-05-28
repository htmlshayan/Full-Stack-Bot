# WEBCAM BOTS

Накрутка ботов на комнату Chaturbate

![Alt Text](/example.jpg)

¯\\_(ツ)_/¯

![Alt Text](/lol.gif)

## Cross-platform setup

Requirements:
- Python 3.9+ (3.10+ recommended)
- Google Chrome or Chromium installed
- Optional: Redis for distributed workers (see docker-compose.yml)

If Chrome is installed in a non-standard location, set one of:
- CHROME_BINARY or CHROME_BIN to the full Chrome/Chromium executable path
- CHROMEDRIVER_PATH to a matching chromedriver binary (optional; undetected_chromedriver can auto-download)

Legacy macOS (10.13/10.14):
- If Chrome launches but ChromeDriver fails, install a compatible chromedriver and set CHROMEDRIVER_PATH.
- If the driver version does not match Chrome, set CHROMEDRIVER_ALLOW_MISMATCH=1 to allow a legacy driver.
- If Chrome still fails to start, set UC_USE_SUBPROCESS=1 to launch via a subprocess.

### Windows (PowerShell)
```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### macOS / Linux (bash/zsh)
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Open http://localhost:3000 in a browser.

### Distributed mode (optional)
Start Redis:
```
docker-compose up -d redis
```

Run a worker (same host or remote host):
```
$env:REDIS_URL = "redis://localhost:6379"
python worker.py
```

On macOS/Linux:
```
export REDIS_URL=redis://localhost:6379
python3 worker.py
```
