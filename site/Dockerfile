FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    SITE_CONTENT_ROOT=/srv/site \
    SITE_STATE_DIR=/data/site \
    SITE_PORT=8080

WORKDIR /srv

# 公告站仅用标准库，无需 pip 安装
COPY site /srv/site
COPY site/server.py /srv/server.py

RUN mkdir -p /data/site
VOLUME ["/data/site"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

CMD ["python", "/srv/server.py"]
