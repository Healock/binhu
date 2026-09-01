FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY schema.sql ./schema.sql

RUN groupadd --system --gid 10001 venue && \
    useradd --system --uid 10001 --gid venue --create-home venue && \
    install -d -o venue -g venue -m 0700 /data/photos
USER venue

EXPOSE 48727
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "48727", "--no-access-log"]
