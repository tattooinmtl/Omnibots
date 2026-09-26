FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.32.1" httpx==0.28.1 websockets==15.0.1 docker==7.1.0 \
 && mkdir /data
WORKDIR /app
COPY app.py .
ENV DATA_DIR=/data
EXPOSE 8080
# Runs as root only because it talks to the Docker socket; it is the ONLY thing that does.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
