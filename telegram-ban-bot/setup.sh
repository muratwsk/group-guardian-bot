#!/bin/bash
# ============================================
# Telegram Ban Bot - Auto Setup Script
# ============================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Telegram Ban Bot - Setup Script${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# 1. System-Updates & Python installieren
echo -e "${YELLOW}[1/5] System aktualisieren & Python installieren...${NC}"
apt update -y && apt install -y python3 python3-pip python3-venv git

# 2. Bot-Verzeichnis einrichten
BOT_DIR="/root/telegram-ban-bot"

if [ ! -d "$BOT_DIR" ]; then
    echo -e "${YELLOW}[2/5] Bot-Verzeichnis erstellen...${NC}"
    mkdir -p "$BOT_DIR"
    echo -e "${RED}Bitte kopiere die Bot-Dateien nach $BOT_DIR und starte das Script erneut!${NC}"
    exit 1
else
    echo -e "${GREEN}[2/5] Bot-Verzeichnis gefunden: $BOT_DIR${NC}"
fi

cd "$BOT_DIR"

# 3. Virtuelle Umgebung & Dependencies
echo -e "${YELLOW}[3/5] Python-Umgebung einrichten...${NC}"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Config prüfen
if [ ! -f "$BOT_DIR/config.json" ]; then
    echo ""
    echo -e "${YELLOW}[4/5] config.json nicht gefunden - wird jetzt erstellt...${NC}"
    echo ""
    
    read -p "Bot Token eingeben: " BOT_TOKEN
    read -p "Deine Telegram User-ID (Owner): " OWNER_ID
    read -p "Log-Channel ID (Enter für keine): " LOG_CHANNEL
    
    if [ -z "$LOG_CHANNEL" ]; then
        LOG_CHANNEL="null"
    fi

    cat > "$BOT_DIR/config.json" <<EOF
{
    "bot_token": "$BOT_TOKEN",
    "owner_ids": [$OWNER_ID],
    "admin_ids": [],
    "log_channel_id": $LOG_CHANNEL
}
EOF
    echo -e "${GREEN}config.json erstellt!${NC}"
else
    echo -e "${GREEN}[4/5] config.json bereits vorhanden ✓${NC}"
fi

# 5. Systemd Service einrichten
echo -e "${YELLOW}[5/5] Systemd Service einrichten...${NC}"

cat > /etc/systemd/system/banbot.service <<EOF
[Unit]
Description=Telegram Ban Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python3 bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable banbot
systemctl start banbot

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ✅ Setup abgeschlossen!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Bot Status:    ${YELLOW}systemctl status banbot${NC}"
echo -e "Bot Logs:      ${YELLOW}journalctl -u banbot -f${NC}"
echo -e "Bot Neustarten:${YELLOW}systemctl restart banbot${NC}"
echo -e "Bot Stoppen:   ${YELLOW}systemctl stop banbot${NC}"
echo ""
echo -e "${GREEN}Der Bot läuft jetzt 24/7! 🚀${NC}"
