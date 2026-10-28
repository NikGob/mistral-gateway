FROM python:3.13-alpine
WORKDIR /app
COPY gateway.py panel.html /app/
RUN adduser -D -u 10001 gateway && mkdir -p /data && chown gateway:gateway /data
USER gateway
ENV BIND=0.0.0.0 PORT=8788 DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/health', timeout=4)" || exit 1
CMD ["python3", "gateway.py"]
