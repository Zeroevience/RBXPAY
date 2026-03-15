@echo off
title RBXLIM Launcher

echo [1/2] Starting trade bot...
start "Trade Bot" cmd /k "cd /d %~dp0 && python trade_bot.py"

echo Waiting for trade bot to initialize...
timeout /t 8 /nobreak >nul

echo [2/2] Starting RBXLIM website...
start "RBXLIM Website" cmd /k "cd /d %~dp0\rbxlim && python app.py"

timeout /t 3 /nobreak >nul
echo.
echo Both servers are starting up.
echo Open http://127.0.0.1:8080 in your browser.
echo.
start "" "http://127.0.0.1:8080"
