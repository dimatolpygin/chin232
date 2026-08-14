FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# ffmpeg нужен для конвертации голосовых (этап 1), ставим сразу — образ не пересобирать.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

COPY . .

RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "docker/entrypoint.sh"]
CMD ["bot"]
