FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLITE_DATABASE_PATH=/data/contentfind.db \
    DOWNLOAD_DIR=/data/downloads \
    BOOKS_DOWNLOAD_DIR=/data/books \
    VIDEOS_DOWNLOAD_DIR=/data/videos \
    RQBIT_URL=http://rqbit:3030
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data \
    && chown app:app /data

COPY . .

USER app

EXPOSE 8000
VOLUME ["/data"]

CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=1", "--threads=4", "app:app"]
