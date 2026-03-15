#!/bin/bash
set -e

echo "[start] Launching trade_bot on port 5000..."
python trade_bot.py &
TRADE_BOT_PID=$!

echo "[start] Waiting for trade_bot to initialise..."
sleep 8

echo "[start] Launching RBXLIM website..."
cd rbxlim
exec python app.py
