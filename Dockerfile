# ====== Dockerfile (Railway / Playwright Chromium) ======
FROM python:3.11-slim

# Empêche Python de buffer les logs (pratique pour Railway)
ENV PYTHONUNBUFFERED=1

# Playwright : stocker les navigateurs dans le container
ENV PLAYWRIGHT_BROWSERS_PATH=0

WORKDIR /app

# Dépendances système + Chromium (Debian)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    libnss3 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libgtk-3-0 \
    libxshmfence1 \
    fonts-noto \
    fonts-dejavu-core \
    fonts-liberation \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installer le navigateur Playwright (SANS --with-deps)
RUN python -m playwright install chromium

# Copier le code
COPY . .

# Commande de lancement (change bot.py si ton fichier a un autre nom)
CMD ["python", "app.py"]
