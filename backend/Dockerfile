FROM python:3.11-slim

WORKDIR /app

# Keep the local deployment mirror. Hosted CI builds can use the upstream indexes.
ARG USE_CHINA_MIRRORS=1
RUN if [ "$USE_CHINA_MIRRORS" = 1 ]; then \
    sed -i -E 's@https?://(deb\.debian\.org|security\.debian\.org)/@https://mirrors.aliyun.com/@g' \
      /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi

# Install build tools and the client used for controlled domain-database backups.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-mysql-client \
    fonts-noto-cjk \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG BINHU_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i "$BINHU_PYPI_INDEX_URL" -r requirements.txt

COPY . .

EXPOSE 37125

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "37125", "--no-access-log"]
