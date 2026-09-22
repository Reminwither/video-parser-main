@echo off
setlocal
title Video Parser / Feishu Bot - Start
cd /d "%~dp0"

echo ============================================
echo   Video Parser / Feishu Transcript Bot
echo ============================================
echo.

rem 1) ensure Ollama (llm cleaning/analysis depends on localhost:11434)
netstat -ano | findstr ":11434" >nul 2>nul
if errorlevel 1 (
  echo [1/3] Ollama is DOWN, starting...
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
     start "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
  ) else if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
     start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
  ) else (
     echo        !! Ollama not found. Install it, or transcript cleaning will be unavailable.
  )
  timeout /t 6 /nobreak >nul
) else (
  echo [1/3] Ollama already running.
)

rem 2) GPU free-mem hint (ASR needs >= 5.5GB)
where nvidia-smi >nul 2>nul
if errorlevel 1 echo [2/3] nvidia-smi not found, skip GPU check.& goto :after_gpu
for /f "tokens=1" %%a in ('nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits') do echo [2/3] GPU free mem: %%a MB (ASR needs ^>=5500)
echo        Close GPU-heavy apps e.g. MasterGo if ASR reports out-of-memory.
:after_gpu

rem 3) port conflict guard
netstat -ano | findstr ":7860" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo Service is already running on port 7860. Do not start twice.
  echo Close the process holding port 7860 first if you want to restart.
  pause
  exit /b 1
)

rem 4) venv check
if not exist ".venv\Scripts\python.exe" (
  echo [3/3] ERROR: .venv not found. Run:
  echo         python -m venv .venv
  echo         .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

echo [3/3] Starting main service. Keep this window open. Ctrl+C to stop.
echo --------------------------------------------------
call ".venv\Scripts\python.exe" app.py

echo.
echo Service exited.
pause