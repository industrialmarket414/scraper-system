#!/bin/bash
# ScrapeNet canlı izleme — sabit başlık + kayan log
cd /home/em-ofis/scraper-system
PID=$(pgrep -f "python main.py" | head -1)
ROWS=$(tput lines)

cleanup() { printf "\033[r\033[2J\033[H"; tput cnorm; exit 0; }
trap cleanup INT TERM

clear
tput civis   # imleci gizle (başlık alanında)

# ── SABİT BAŞLIK (üst 13 satır) ──
tput cup 0 0
echo -e "\033[1;36m╔══════════════════════════════════════════════════════════════╗\033[0m"
echo -e "\033[1;36m║              SCRAPENET — CANLI SİSTEM İZLEME                  ║\033[0m"
echo -e "\033[1;36m╚══════════════════════════════════════════════════════════════╝\033[0m"
echo -e "  \033[1;32m●\033[0m Sunucu PID        : \033[1;33m${PID:-KAPALI}\033[0m"
echo -e "  \033[1;32m●\033[0m Adres             : \033[1;34mhttp://scrapenet:8000\033[0m"
echo -e "  \033[1;32m●\033[0m Paneller          : Panel 1 (B2B Stok)  |  Panel 2 (Fiyat Analiz)"
echo -e "  \033[1;32m●\033[0m Otomatik Çekim    : Her gece \033[1;33m03:00\033[0m  (9 B2B + 5 fiyat sitesi)"
echo -e "  \033[1;31m●\033[0m Bilgisayar Şifresi: \033[1;37memofis\033[0m"
echo ""
echo -e "\033[1;36m─────────────  GELEN İSTEKLER / CEVAPLAR (canlı)  ─────────────\033[0m"

# ── KAYAN BÖLGE: 13. satırdan ekran sonuna ──
printf "\033[13;%dr" "$ROWS"   # scroll region tanımla
tput cup 12 0                   # imleci kayan bölgenin başına al
tput cnorm                      # imleci göster (log alanında)

tail -n 12 -f logs/service.log
