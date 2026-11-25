#!/bin/bash
# Run the agent automatically with launchd (the Mac's built-in scheduler).
#   ./schedule.sh install 07:45          # every day at 7:45 AM (default 07:45)
#   ./schedule.sh install-startup 07:45  # when you log in, and at 7:45 if you haven't; once a day at most
#   ./schedule.sh remove                 # stop automatic runs
#   ./schedule.sh status                 # is it scheduled? when did it last run?
#   ./schedule.sh now                    # run the scheduled job right away (for testing)
cd "$(dirname "$0")" || exit 1
DIR=$(pwd)
LABEL="com.jobagent.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

write_plist() {   # $1 = HH:MM, $2 = "startup" or "daily"
  TIME="$1"
  case "$TIME" in
    [0-9]:[0-5][0-9]|[01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
    *) echo "Time should look like 07:45 (24-hour)."; exit 1 ;;
  esac
  HOUR=$((10#${TIME%%:*})); MIN=$((10#${TIME##*:}))
  case "$DIR" in
    "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
      echo "Note: macOS blocks scheduled jobs from reading Documents, Desktop and Downloads"
      echo "unless you grant Full Disk Access. Moving this folder to ~/JobAgent avoids that." ;;
  esac
  if [ "$2" = "startup" ]; then
    EXTRA="    <string>--once-a-day</string>"
    ATLOAD="  <key>RunAtLoad</key><true/>"
  else
    EXTRA=""; ATLOAD=""
  fi
  mkdir -p "$HOME/Library/LaunchAgents" logs
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run.sh</string>
$EXTRA
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
$ATLOAD
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MIN</integer>
  </dict>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
}

case "${1:-}" in
  install)
    write_plist "${2:-07:45}" daily
    printf "Scheduled daily at %02d:%02d. Log: %s/logs/launchd.log\n" "$HOUR" "$MIN" "$DIR"
    ;;
  install-startup)
    write_plist "${2:-07:45}" startup
    printf "Scheduled: runs when you log in, and at %02d:%02d if it hasn't run yet that day.\n" "$HOUR" "$MIN"
    echo "It runs at most once a day, and starts a first run now. Log: $DIR/logs/launchd.log"
    ;;
  remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Automatic runs removed."
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      if grep -q RunAtLoad "$PLIST" 2>/dev/null; then
        echo "Scheduled: at login and daily, once a day at most."
      else
        echo "Scheduled: daily at a set time."
      fi
      launchctl print "$DOMAIN/$LABEL" | grep -E "state|last exit code|runs" || true
    else
      echo "Not scheduled."
    fi
    [ -f logs/last_run ] && echo "Last completed scheduled run: $(cat logs/last_run)"
    ;;
  now)
    rm -f logs/last_run          # so the once-a-day check doesn't skip this test run
    launchctl kickstart -p "$DOMAIN/$LABEL" && echo "Started; watch logs/launchd.log"
    ;;
  *)
    sed -n '2,7p' "$0"; exit 1 ;;
esac
