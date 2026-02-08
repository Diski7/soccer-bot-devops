#!/bin/bash
source .env
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python soccer_bot.py
