FROM python:3.12-slim
WORKDIR /app
# The bot starts as root, gives the mounted data folder to this user and then runs as it (privileges.py).
# uid 1000 is usually the host's first account, so the data folder stays easy to back up there.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /app --shell /usr/sbin/nologin bot
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY *.py ./
COPY assets/ ./assets/
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/checklist.db HEALTH_PORT=8080 BOT_USER=bot
VOLUME /data
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('HEALTH_PORT','8080'), timeout=4)"
CMD ["python", "bot.py"]
