FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/checklist.db HEALTH_PORT=8080
VOLUME /data
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('HEALTH_PORT','8080'), timeout=4)"
CMD ["python", "bot.py"]
