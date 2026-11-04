FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Kyiv

WORKDIR /app

# Встановлюємо шрифти з підтримкою кирилиці для генерації зображень графіка (Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "-m", "app.main"]
