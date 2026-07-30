FROM python:3.11-slim

WORKDIR /app

# 换国内镜像源加速（apt 阿里云 + pip 阿里云）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

# Install build tools and the client used for controlled three-database backups.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-mysql-client \
    fonts-noto-cjk \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY . .

EXPOSE 37125

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "37125"]
