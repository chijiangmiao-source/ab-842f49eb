FROM python:3.11-slim

WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "app.server"]
