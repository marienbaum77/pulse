#!/bin/bash
# Бэкап Postgres: раз в BACKUP_INTERVAL_HOURS часов (по умолчанию 24), без лишних дампов при перезапуске контейнера.
# Векторы (эмбеддинги, центроиды) в дамп не попадают: они занимают ~95% объёма и пересчитываются сами после восстановления.
# Хранятся не более BACKUP_KEEP_COUNT последних файлов и не старше BACKUP_KEEP_DAYS суток.
set -u
set -o pipefail

DIR=/backups
INTERVAL=$(( ${BACKUP_INTERVAL_HOURS:-24} * 3600 ))
KEEP_COUNT=${BACKUP_KEEP_COUNT:-7}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
STRIP=${BACKUP_STRIP_VECTORS:-true}
AWK_FILTER=/usr/local/share/strip-vectors.awk

filter() {
  if [ "$STRIP" = "true" ]; then awk -f "$AWK_FILTER"; else cat; fi
}

cleanup() {
  rm -f "$DIR"/pulse-*.sql.gz.tmp
  ls -1t "$DIR"/pulse-*.sql.gz 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -f
  find "$DIR" -name 'pulse-*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null
}

next_wait() {
  local last now age
  last=$(ls -1t "$DIR"/pulse-*.sql.gz 2>/dev/null | head -n 1)
  [ -z "$last" ] && { echo 60; return; }
  now=$(date +%s)
  age=$(( now - $(stat -c %Y "$last") ))
  if [ "$age" -ge "$INTERVAL" ]; then echo 60; else echo $(( INTERVAL - age )); fi
}

echo "Pulse backup: каталог $DIR, интервал $((INTERVAL / 3600)) ч, храним $KEEP_COUNT файлов / $KEEP_DAYS сут., без векторов: $STRIP"
cleanup
sleep "$(next_wait)"

while true; do
  ts=$(date +%F_%H%M)
  out="$DIR/pulse-$ts.sql.gz"
  if pg_dump -h db -U pulse -d pulse --exclude-table-data=jobs --exclude-table-data=llm_calls | filter | gzip -6 > "$out.tmp"; then
    mv "$out.tmp" "$out"
    echo "Создан $out ($(du -h "$out" | cut -f1))"
  else
    echo "Ошибка pg_dump, бэкап пропущен" >&2
    rm -f "$out.tmp"
  fi
  cleanup
  sleep "$INTERVAL"
done
