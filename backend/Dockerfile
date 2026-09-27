FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ocrmypdf tesseract-ocr-rus poppler-utils libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
RUN apt-get update && apt-get install -y --no-install-recommends lame && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 dispatcher && useradd --uid 10001 --gid dispatcher --create-home --shell /usr/sbin/nologin dispatcher
COPY . .
ENV XDG_CACHE_HOME=/tmp/cache
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-keep-alive", "30"]
