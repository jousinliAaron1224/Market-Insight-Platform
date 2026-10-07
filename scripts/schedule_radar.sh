#!/bin/sh
# 定期自動更新商品情報雷達（macOS launchd）：每天 08:00、18:00 跑 scripts/update_radar.py --push
#
#   sh scripts/schedule_radar.sh install     # 安裝並啟用排程
#   sh scripts/schedule_radar.sh run         # 立刻跑一次（用排程的同一個設定，可用來測試）
#   sh scripts/schedule_radar.sh status      # 排程狀態與最近一次結果
#   sh scripts/schedule_radar.sh uninstall   # 移除排程
#
# 注意：專案放在「桌面」時，macOS 會擋背景程式讀桌面資料夾（Operation not permitted）。
# 第一次 run 後若 data/logs/launchd.err 出現這個錯誤，請到「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」
# 加入 .venv/bin/python 指向的 Python 執行檔（status 會印出路徑），或把兩個專案移出桌面。
set -eu

LABEL=tw.insurance-intel.update-radar
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$ROOT/data/logs"
HOURS=${RADAR_HOURS:-"8 18"}   # 可用環境變數改時間，例如 RADAR_HOURS="7 12 19"

write_plist() {
  mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/scripts/update_radar.py</string>
    <string>--push</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key>
  <array>
EOF
    for h in $HOURS; do
      printf '    <dict><key>Hour</key><integer>%s</integer><key>Minute</key><integer>0</integer></dict>\n' "$h"
    done
    cat <<EOF
  </array>
  <key>StandardOutPath</key><string>$LOGS/launchd.out</string>
  <key>StandardErrorPath</key><string>$LOGS/launchd.err</string>
</dict>
</plist>
EOF
  } > "$PLIST"
}

case "${1:-status}" in
  install)
    [ -x "$PY" ] || { echo "找不到 $PY，請先建立虛擬環境：python3 -m venv .venv && .venv/bin/pip install -e ."; exit 1; }
    write_plist
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "已安裝：每天 $(echo $HOURS | sed 's/ /、/g') 點執行 update_radar.py --push"
    echo "設定檔：$PLIST"
    ;;
  run)
    launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "已觸發，進度看：tail -f \"$LOGS/launchd.err\""
    ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state =|last exit code|runs =" || echo "排程未安裝"
    echo "Python：$(readlink -f "$PY" 2>/dev/null || echo "$PY")"
    [ -f "$LOGS/last_update.json" ] && { echo "最近一次結果（data/logs/last_update.json）："; head -c 1500 "$LOGS/last_update.json"; echo; } || true
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已移除排程"
    ;;
  *) echo "用法：sh scripts/schedule_radar.sh install|run|status|uninstall"; exit 1 ;;
esac
