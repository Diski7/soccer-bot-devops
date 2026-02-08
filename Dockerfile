FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY soccer_bot.py .
RUN mkdir -p /tmp
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 CMD python -c "import requests; requests.get('https://api.telegram.org/bot' + __import__('os').getenv('TELEGRAM_TOKEN') + '/getMe')" || exit 1
CMD ["python", "soccer_bot.py"]
