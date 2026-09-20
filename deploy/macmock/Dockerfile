FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir Flask==3.1.1 gunicorn==23.0.0
WORKDIR /app
COPY app.py /app/app.py
RUN useradd --create-home --uid 10001 macmock \
    && mkdir -p /data \
    && chown -R macmock:macmock /app /data
USER macmock
EXPOSE 23333
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:23333/health', timeout=2)"
ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:23333", "--workers", "2", "--threads", "2", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
