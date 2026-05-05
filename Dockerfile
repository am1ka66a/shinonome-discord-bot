# 精簡映像 + 依賴獨立層：requirements 未改時可重用快取，縮短 Railway 建置時間
# 使用 3.13：audioop-lts 僅提供 3.13+ 的 wheel，與 requirements 一致
FROM python:3.13-slim-bookworm

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
