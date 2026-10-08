# syntax=docker/dockerfile:1

ARG BASE_IMAGE=immich-video-optimizer-base:hb1.11.2
FROM ${BASE_IMAGE}

COPY app ./app
COPY tests ./tests

RUN python3 -m unittest discover -s tests -v

USER root

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8090') + '/api/status', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec gunicorn --chdir /app --bind 0.0.0.0:${PORT:-8090} --workers 1 --threads 4 --worker-class gthread --timeout 0 --access-logfile - app.web:app"]
