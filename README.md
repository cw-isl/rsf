# Smart Frame (sfmark3)

This project provides a web dashboard and Telegram bot for displaying calendar, weather,
tasks, and photos on a smart frame.

## Requirements
- Python 3.9+
- Access to the internet for weather, Todoist, Google Calendar, and Telegram APIs
- Optional: a public HTTPS endpoint if using Telegram webhook mode

## Installation
```bash
sudo apt update
sudo apt install -y python3 python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install flask requests pyTelegramBotAPI google-auth google-auth-oauthlib google-api-python-client
```

## Running
```bash
source .venv/bin/activate
python sfmark3.py
```
The server listens on port `5320` and serves the board at `http://<host>:5320/board`.
Photos for the background slideshow are loaded from `/root/rsf/photo`.

## Network configuration
- **Web server**: allow inbound traffic on port `5320` from devices that should view the board. Configure
  your firewall or router to forward this port if access is required from outside your network.
- **Telegram bot**: default mode is polling and only requires outbound internet access. To use webhook mode,
  set `telegram.mode` to `"webhook"` and provide a publicly reachable HTTPS URL in `telegram.webhook_base`; ensure this
  URL forwards requests to the server's `:5320` port.
