# 精簡映像 + 依賴獨立層：requirements 未改時可重用快取，縮短 Railway 建置時間
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY data ./data

CMD ["python", "-u", "bot.py"]
