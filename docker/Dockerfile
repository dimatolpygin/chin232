FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# ffmpeg нужен для конвертации голосовых (этап 1), ставим сразу — образ не пересобирать.
#
# postgresql-client — ради pg_dump и psql: ими снимается и восстанавливается
# ночная копия базы. Штатный клиент debian подходит, потому что база у нас 16,
# а в trixie лежит 17: клиент новее сервера дампит нормально, наоборот —
# отказывается. Сторонний репозиторий postgres поэтому не нужен.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl tini postgresql-client \
 && rm -rf /var/lib/apt/lists/*

# Проверка на месте сборки: если базовый образ однажды съедет на debian с
# клиентом старше нашего сервера, узнать об этом надо здесь, а не ночью, когда
# копия молча не соберётся.
# Имя переменной латиницей намеренно: sh внутри образа кириллических
# идентификаторов не понимает, как и bash на раннере GitHub Actions.
RUN set -e; \
    major="$(pg_dump --version | sed -E 's/.* ([0-9]+).*/\1/')"; \
    echo "pg_dump мажорной версии $major"; \
    [ "$major" -ge 16 ] || { echo "pg_dump старше базы (нужен >= 16)"; exit 1; }

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

COPY . .

RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "docker/entrypoint.sh"]
CMD ["bot"]
