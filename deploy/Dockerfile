FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY pyproject.toml README.md ./
COPY goldcopier ./goldcopier
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home copier && mkdir /data && chown copier:copier /data
USER copier
ENV PLATFORM_DB=/data/platform.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn","goldcopier.platform:create_app","--factory","--host","0.0.0.0","--port","8000","--proxy-headers","--forwarded-allow-ips=*"]
