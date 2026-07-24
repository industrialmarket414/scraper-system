#!/bin/bash
export HOME=/home/em-ofis
export PLAYWRIGHT_BROWSERS_PATH=/home/em-ofis/.cache/ms-playwright
export DISPLAY=:0

cd /home/em-ofis/scraper-system
mkdir -p logs

# Eski process'i durdur
pkill -f "python main.py" 2>/dev/null || true
sleep 1

nohup ./venv/bin/python main.py >> logs/service.log 2>&1 &
echo $! > logs/scrapenet.pid
echo "ScrapeNet başlatıldı. PID: $!"
