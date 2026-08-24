#!/usr/bin/env bash
# Первичная настройка чистого сервера под china_bot.
#
# Всё, что этот скрипт делает, раньше жило только в голове и на самом сервере:
# docker, swap, лимиты логов, Caddy с сертификатом, ночной локальный дамп. При
# потере машины это пришлось бы вспоминать заново — теперь оно в репозитории и
# ставится одной командой.
#
# Ubuntu 24.04, запускать от root:
#
#   git clone https://github.com/dimatolpygin/chin232 /opt/china_bot
#   cd /opt/china_bot && bash deploy/bootstrap.sh
#
# Скрипт идемпотентный: повторный запуск ничего не ломает. Он НЕ трогает базу
# и НЕ создаёт .env — секреты кладутся руками из dostupi.txt, а данные
# поднимаются отдельной командой восстановления (см. docs/08_DEPLOY.md).

# Имена переменных здесь латиницей: bash не принимает кириллические
# идентификаторы и падает с «not a valid identifier».
set -euo pipefail

PROJECT=/opt/china_bot
SWAPFILE=/swapfile
SWAP_GB=2

step() { echo; echo "=== $* ==="; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Запускать от root." >&2
  exit 1
fi

step "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git gnupg debian-keyring debian-archive-keyring \
  apt-transport-https

step "Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin
else
  echo "docker уже стоит: $(docker --version)"
fi

step "Ограничение логов docker"
# Без него json-file пишет без предела и однажды забивает диск.
install -D -m 0644 "$PROJECT/deploy/daemon.json" /etc/docker/daemon.json
systemctl restart docker

step "Своп"
# Двух гигабайт памяти не хватает на сборку образа, и она падает молча.
if ! swapon --show | grep -q "$SWAPFILE"; then
  fallocate -l "${SWAP_GB}G" "$SWAPFILE"
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE" >/dev/null
  swapon "$SWAPFILE"
  grep -q "^$SWAPFILE" /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
else
  echo "своп уже включён"
fi

step "Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi
install -D -m 0644 "$PROJECT/deploy/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /var/log/caddy
systemctl reload caddy || systemctl restart caddy

step "Ночной локальный дамп"
# Второй уровень копий, рядом с базой: из соседней папки восстановиться быстрее,
# чем из сети. Копия в S3 при этом снимается сама, задачей воркера.
mkdir -p /opt/backup
install -m 0755 "$PROJECT/deploy/backup/pg_dump.sh" /opt/backup/pg_dump.sh
install -m 0644 "$PROJECT/deploy/backup/china_backup" /etc/cron.d/china_backup

step "Что осталось сделать руками"
cat <<'ПАМЯТКА'
1. Положить /opt/china_bot/.env — из dostupi.txt. Без него ничего не поднимется.
2. Проверить, что A-запись домена смотрит на этот сервер (сертификат Caddy
   получает сам, но только когда домен уже указывает сюда).
3. Поднять контейнеры:
     cd /opt/china_bot
     docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
4. Накатить последнюю копию базы из хранилища:
     docker compose -f docker-compose.yml -f docker-compose.prod.yml \
       exec -e PYTHONPATH=/app worker python -m app.tools.s3backup список
     docker compose -f docker-compose.yml -f docker-compose.prod.yml \
       exec -e PYTHONPATH=/app worker \
       python -m app.tools.s3backup восстановить ИМЯ_ФАЙЛА --да
5. Перезапустить бота и воркер, проверить https://chinesetonebot.ru/health
6. Для автовыкатки: положить публичную часть ключа выкатки в
   /root/.ssh/authorized_keys и обновить секрет SERVER_HOST_KEY в GitHub.
ПАМЯТКА
