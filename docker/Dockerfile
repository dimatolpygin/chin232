FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# ffmpeg нужен для конвертации голосовых (этап 1), ставим сразу — образ не пересобирать.
#
# postgresql-client берём из репозитория самого postgres, а не из debian: там
# лежит версия 15, а сервер у нас 16, и pg_dump отказывается снимать дамп с
# сервера новее себя. Без него ночная копия базы не соберётся.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl tini gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

COPY . .

RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "docker/entrypoint.sh"]
CMD ["bot"]
