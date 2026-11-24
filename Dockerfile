FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARCHIVE_DB_PATH=/data/archive.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单进程多线程：SQLite WAL + busy_timeout 保证读写并发安全，
# 写事务（BEGIN IMMEDIATE）在库级串行化，读走快照。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
