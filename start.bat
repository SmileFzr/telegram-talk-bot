@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Telegram Relay Bot / Chuan Hua Tong
echo ============================================
echo.
echo Checking dependencies...
python -c "import telegram, dotenv" 2>nul
if errorlevel 1 (
  echo Dependencies missing, installing...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Cannot install dependencies. Check network and try again.
    pause
    exit /b 1
  )
)
if not exist ".env" (
  echo [WARNING] .env not found. Copy .env.example to .env and fill in TOKEN.
)
echo.
echo Press Ctrl+C to stop the bot.
echo.
python bot.py
echo.
echo Bot stopped. Press any key to close this window.
pause >nul
