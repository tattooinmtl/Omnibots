FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.32.1" httpx==0.28.1 \
 && useradd --system --uid 10001 --create-home gateway \
 && mkdir /data && chown gateway /data
WORKDIR /app
COPY gateway.py .
USER gateway
ENV DATA_DIR=/data SEARXNG_URL=http://searxng-fc:8080
EXPOSE 8080
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
