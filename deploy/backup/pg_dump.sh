#!/usr/bin/env sh
# Суточный дамп боевой базы. Хранится неделя, дальше затирается.
set -e
stamp=$(date +%Y-%m-%d)
out=/opt/backup/china_bot-$stamp.sql.gz
docker exec china_postgres pg_dump -U china_bot -d china_bot | gzip > "$out"
find /opt/backup -name 'china_bot-*.sql.gz' -mtime +7 -delete
echo "$(date '+%d.%m.%Y %H:%M') дамп готов: $out ($(du -h "$out" | cut -f1))" >> /opt/backup/backup.log
