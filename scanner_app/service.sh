#!/bin/bash
# จัดการ launchd service ของระบบสแกนใบเบิกวัตถุดิบ
# ใช้:  ./service.sh install | uninstall | start | stop | restart | status | logs
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.nsl.scanner"
PLIST_SRC="$APP_DIR/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "$1" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "✓ ติดตั้ง service แล้ว — เปิดเครื่องมาจะรันเองที่ http://127.0.0.1:8000"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✓ ถอน service แล้ว"
    ;;
  start)   launchctl kickstart "$DOMAIN/$LABEL"; echo "✓ เริ่มทำงาน" ;;
  stop)    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; echo "✓ หยุดแล้ว" ;;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL"; echo "✓ รีสตาร์ทแล้ว" ;;
  status)  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|pid" || echo "service ยังไม่ได้ติดตั้ง/ไม่ทำงาน" ;;
  logs)    tail -n 40 "$APP_DIR/logs/service.log" "$APP_DIR/logs/service.err.log" 2>/dev/null ;;
  *)
    echo "ใช้: ./service.sh install | uninstall | start | stop | restart | status | logs"
    ;;
esac
