FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances Chromium (Playwright)
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpangocairo-1.0-0 libasound2 libxshmfence1 \
    libx11-xcb1 libxkbcommon0 libgtk-3-0 \
    fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Installer Chromium via Playwright
RUN python -m playwright install --with-deps chromium

COPY . .

CMD ["python", "app.py"]
