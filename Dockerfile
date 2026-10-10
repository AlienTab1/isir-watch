FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ISIR_DB_PATH=/data/isir-watch.db TZ=Europe/Prague PORT=80
WORKDIR /opt/isir-watch
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home isir && mkdir /data && chown isir:isir /data
COPY app ./app
USER isir
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:80/health',timeout=4)" || exit 1
CMD ["python", "-m", "app.run"]
