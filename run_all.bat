@echo off
setlocal

rem Optional: set a specific Python executable
rem set "PYTHON=C:\Path\To\python.exe"
if not defined PYTHON set "PYTHON=python"

set "ROOT=%~dp0"
set "IG_DIR=%ROOT%Instagram Bot"
set "CB_DIR=%ROOT%webcam-bots"
set "ORCH_DIR=%ROOT%orchestrator"


start "IG Server" cmd /k "cd /d \"%IG_DIR%\" & %PYTHON% run_server_tunnel.py"
start "CB Server" cmd /k "cd /d \"%CB_DIR%\" & %PYTHON% main.py"
start "Orchestrator" cmd /k "cd /d \"%ORCH_DIR%\" & set ORCH_CONFIG=%ORCH_DIR%config.json & %PYTHON% orchestrator.py"

start "Cloudflared Tunnel 1" cmd /k "cloudflared tunnel run --token eyJhIjoiMTQ0NWVjMzBkY2M2MGI2NmRkNWQ4ZTAzMGMzNzkxZTIiLCJ0IjoiNTIwMWU3MDQtODk2OC00YjU3LTkxYTQtNjFiNDliZTNjNGUxIiwicyI6Ik5UWTNZbU16TkdVdE16ZG1NeTAwWWpSaExUbG1ORGN0WXpZelkyVXhNV1JtT1RrdyJ9"
start "Cloudflared Tunnel 2" cmd /k "cloudflared tunnel run --token eyJhIjoiMTQ0NWVjMzBkY2M2MGI2NmRkNWQ4ZTAzMGMzNzkxZTIiLCJ0IjoiNTA3ZTQyNWEtZjQzOS00Zjc5LWExYTgtZjFhZmE0ZmIwYjZkIiwicyI6Ik5qQmpObU0yWXpZdE5ETmxZeTAwTjJReUxXSXdOalV0WVdFd01qRTBZbUV5WVRCaCJ9"

endlocal
