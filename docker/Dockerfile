FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# Версия клиента postgres. Держать её надо РОВНО такой же, как у сервера в
# docker-compose.yml (postgres:16-alpine): меняете там — меняйте и здесь.
#
# Почему не «любой клиент поновее»: pg_dump 17 пишет в заголовок дампа строку
# SET transaction_timeout, которой в шестнадцатом сервере ещё нет, и такой дамп
# на него не накатывается — psql останавливается на первой же команде. Копия
# при этом снимается без единой жалобы, и выясняется всё в тот день, когда
# восстанавливаться уже надо. Поймано живой проверкой восстановления.
ARG PG_MAJOR=16

# ffmpeg нужен для конвертации голосовых (этап 1), ставим сразу — образ не пересобирать.
#
# postgresql-client — ради pg_dump и psql: ими снимается и восстанавливается
# ночная копия базы. Берём из репозитория самого postgres: в debian лежит
# только та версия, что совпала с релизом дистрибутива, а нам нужна версия
# сервера. Кодовое имя дистрибутива определяем на месте — базовый образ уже
# переезжал с bookworm на trixie, и зашитое имя ломало сборку.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl tini gnupg \
 && . /etc/os-release \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Проверка на месте сборки: версия клиента обязана совпадать с версией сервера.
# Разъедется — узнать об этом надо здесь, а не ночью, когда копия снимется
# нерабочей.
# Имя переменной латиницей намеренно: sh внутри образа кириллических
# идентификаторов не понимает, как и bash на раннере GitHub Actions.
RUN set -e; \
    major="$(pg_dump --version | sed -E 's/.* ([0-9]+).*/\1/')"; \
    echo "pg_dump мажорной версии $major, сервер $PG_MAJOR"; \
    [ "$major" = "$PG_MAJOR" ] || { echo "клиент postgres разъехался с сервером"; exit 1; }

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

COPY . .

RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "docker/entrypoint.sh"]
CMD ["bot"]
