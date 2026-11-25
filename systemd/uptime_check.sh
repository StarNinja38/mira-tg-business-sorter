#!/bin/bash
# Uptime monitor (cron */5). systemd handles normal restarts (Restart=on-failure).
# This catches crash-loops (StartLimitBurst hit) and hard stops: alerts (deduped 30m) + gentle recovery.
source /opt/tg-business-sorter/.env

if systemctl is-active --quiet tg-sorter; then
  exit 0
fi

ALERT_STAMP=/tmp/tg_uptime_alert
NOW=$(date +%s)
LAST=0
[ -f "$ALERT_STAMP" ] && LAST=$(cat "$ALERT_STAMP" 2>/dev/null || echo 0)

# Alert at most once per 30 min to avoid spam during a crash-loop
if [ $((NOW - LAST)) -ge 1800 ]; then
  curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${OWNER_USER_ID}" \
    -d "text=🚨 Бот tg-sorter не работает. Пробую восстановить. Логи: journalctl -u tg-sorter -n 50" >/dev/null
  echo "$NOW" > "$ALERT_STAMP"
fi

# Gentle recovery: clear crash-loop counter, then start (no aggressive restart loop)
systemctl reset-failed tg-sorter 2>/dev/null
systemctl start tg-sorter 2>/dev/null
