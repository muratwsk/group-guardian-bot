# Telegram Ban Bot

Ein einfacher Telegram-Bot zum Bannen/Entbannen von Usern über mehrere Gruppen gleichzeitig.

## Features

- 🚫 **Ban/Unban** in einzelnen oder allen Gruppen per Button-Menü
- 📋 **Gruppen anzeigen** – alle registrierten Gruppen auflisten
- `/banall` – Antworte auf eine Nachricht in einer Gruppe → User wird in allen Gruppen gebannt
- `/unbanall` – Gleiches für Unban
- 👤 **Admin-Verwaltung** – Admins per Bot hinzufügen/entfernen
- 📢 **Log-Kanal** – Alle Aktionen in einen externen Kanal loggen

## Setup

### 1. Bot erstellen
1. Öffne [@BotFather](https://t.me/BotFather) auf Telegram
2. `/newbot` → Namen & Username wählen
3. Token kopieren

### 2. Konfiguration
Bearbeite `config.json`:
```json
{
  "bot_token": "DEIN_BOT_TOKEN",
  "admin_ids": [DEINE_TELEGRAM_USER_ID],
  "log_channel_id": null
}
```

> Deine User-ID findest du über [@userinfobot](https://t.me/userinfobot)

### 3. Installation (VPS)
```bash
git clone <dein-repo>
cd telegram-ban-bot
pip install -r requirements.txt
python bot.py
```

### 4. Als Service laufen lassen (systemd)
```bash
sudo nano /etc/systemd/system/banbot.service
```
```ini
[Unit]
Description=Telegram Ban Bot
After=network.target

[Service]
User=root
WorkingDirectory=/pfad/zu/telegram-ban-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable banbot
sudo systemctl start banbot
```

## Befehle

| Befehl | Wo | Beschreibung |
|---|---|---|
| `/start` | Privat | Hauptmenü öffnen |
| `/registergroup` | Gruppe | Gruppe beim Bot registrieren |
| `/unregistergroup` | Gruppe | Gruppe entfernen |
| `/banall` | Gruppe | Auf Nachricht antworten → User überall bannen |
| `/unbanall` | Gruppe | Auf Nachricht antworten → User überall entbannen |

## Wichtig
- Der Bot muss **Admin** in allen Gruppen sein (mit Ban-Rechten)
- Log-Kanal: Bot muss dort als Admin hinzugefügt sein
- Gruppen werden per `/registergroup` registriert (in der Gruppe ausführen)
