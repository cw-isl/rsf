#!/usr/bin/env bash
set -e

# Create virtual environment if not exists
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install flask requests pyTelegramBotAPI google-auth google-auth-oauthlib google-api-python-client
python sfmark3.py
