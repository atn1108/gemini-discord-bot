#!/usr/bin/env bash
# Cài Docker, tạo 1GB swap (bắt buộc cho e2-micro 1GB RAM), và chạy bot.
# Dùng trên VM Ubuntu/Debian mới tạo:
#   git clone https://github.com/atn1108/gemini-discord-bot.git && cd gemini-discord-bot
#   cp .env.example .env   # điền token/DATABASE_URL
#   bash deploy-gcp.sh
set -euo pipefail

# 1. Swap 1GB cho e2-micro (tránh OOM khi build image hoặc chạy yt-dlp)
if [ ! -f /swapfile ] && [ "$(swapon --show | wc -l)" -eq 0 ]; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "[ok] Added 1G swap"
fi

# 2. Cài Docker + Compose plugin nếu chưa có
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "[ok] Installed Docker"
fi

if [ ! -f .env ]; then
  echo "[err] Chưa có file .env. Chạy: cp .env.example .env rồi điền biến môi trường trước."
  exit 1
fi

sudo docker compose up -d --build
sudo docker compose ps
echo "[ok] Bot started. Xem log: sudo docker compose logs -f gemini-discord-bot"
