#!/bin/bash
# E2E test harness for Maimchat. Source this, then call the helpers.
# Usage: DEV=emu|plj source harness.sh   (default emu)
set -u
PKG=com.l2dchat
ENGINE=com.l2dchat.shell
ADBBASE=/home/tcmofashi/android-sdk/platform-tools/adb
case "${DEV:-emu}" in
  plj) ADB="$ADBBASE -H 127.0.0.1 -P 15037 -s 3B162500M2F00000" ;;
  *)   ADB="$ADBBASE -s emulator-5554" ;;
esac
DBT=$(mktemp -d)

# ---- input / clear (debug TestControlReceiver) ----
send()       { $ADB shell "am broadcast -p $PKG -a com.l2dchat.test.SEND --es text \"$1\"" >/dev/null 2>&1; }
clear_chat() { $ADB shell "am broadcast -p $PKG -a com.l2dchat.test.CLEAR" >/dev/null 2>&1; sleep 3; }
launch()     { $ADB shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1; $ADB shell am start -n $PKG/.MainActivity >/dev/null 2>&1; sleep 4; }
clear_log()  { $ADB shell "run-as $PKG sh -c 'rm -f files/logs/l2dchat.log'" >/dev/null 2>&1; }

# ---- records (DUMP via adb: DB + log) ----
_pulldb()    { rm -f "$DBT"/*; for f in maimchat_runtime.db maimchat_runtime.db-wal maimchat_runtime.db-shm; do $ADB exec-out run-as $PKG cat /data/data/$PKG/databases/$f > "$DBT/$f" 2>/dev/null; done; }
msgs()       { _pulldb; sqlite3 "$DBT/maimchat_runtime.db" "SELECT (CASE WHEN sender_user_id='gentle' THEN 'A' ELSE 'U' END)||': '||substr(replace(raw_text,char(10),' '),1,55) FROM standard_messages ORDER BY timestamp_ms;" 2>/dev/null; }
asst_count() { _pulldb; sqlite3 "$DBT/maimchat_runtime.db" "SELECT count(*) FROM standard_messages WHERE sender_user_id='gentle';" 2>/dev/null; }
rounds()     { _pulldb; sqlite3 "$DBT/maimchat_runtime.db" "SELECT state FROM planner_rounds ORDER BY created_at_ms DESC LIMIT 6;" 2>/dev/null; }
timing()     { $ADB shell "run-as $PKG cat files/logs/l2dchat.log 2>/dev/null" 2>&1 | grep '\[timing\]' | sed -E 's/^.*CHAT\|//; s/\|$//'; }
errlog()     { $ADB shell "run-as $PKG cat files/logs/l2dchat.log 2>/dev/null" 2>&1 | grep -iE 'ERROR|exception|Unknown tool' | tail -3; }
turns_done() { timing | grep -c 'turn done'; }
replies_sent(){ timing | grep -c 'replySent=true'; }
has_file()   { _pulldb; sqlite3 "$DBT/maimchat_runtime.db" "SELECT count(*) FROM standard_messages WHERE message_json LIKE '%fileInfo%' OR message_json LIKE '%agent_files%';" 2>/dev/null; }

# ---- worker / process state ----
worker_n()   { $ADB shell "ps -A 2>/dev/null | grep -cE 'proot|python3'" | tr -d '\r'; }
main_pid()   { $ADB shell "pidof $PKG" 2>&1 | tr -d '\r'; }
chat_pid()   { $ADB shell "pidof $PKG:chat" 2>&1 | tr -d '\r'; }
engine_pid() { $ADB shell "pidof $ENGINE" 2>&1 | tr -d '\r'; }

# ---- freeze / kill (system simulation) ----
home()       { $ADB shell input keyevent 3 >/dev/null 2>&1; sleep 1; }       # background to launcher
kill_main()  { $ADB shell am kill $PKG >/dev/null 2>&1; }                     # kill cached main process (bg only)
force_stop() { $ADB shell am force-stop $PKG >/dev/null 2>&1; }               # stop all app processes (engine separate)
freeze()     { $ADB shell "am make-uid-idle $PKG" >/dev/null 2>&1; $ADB shell "cmd deviceidle force-idle" >/dev/null 2>&1; }
unfreeze()   { $ADB shell "cmd deviceidle unforce" >/dev/null 2>&1; }

# ---- wait helpers ----
# wait_turn N: wait up to N*3s for at least one 'turn done'
wait_turn()  { for i in $(seq 1 ${1:-10}); do sleep 3; [ "$(turns_done)" -ge "${2:-1}" ] && return 0; done; return 1; }
# wait_worker_done N: wait up to N*6s for the worker (python3) to have run AND finished
wait_worker(){ local saw=0; for i in $(seq 1 ${1:-60}); do sleep 6; [ "$(worker_n)" -gt 0 ] && saw=1; [ "$saw" = 1 ] && [ "$(worker_n)" = 0 ] && return 0; done; return 1; }

# ---- assertions ----
PASS=0; FAIL=0
ok()   { echo "   PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "   FAIL: $1"; FAIL=$((FAIL+1)); }
chk()  { if eval "$1"; then ok "$2"; else bad "$2  [$1]"; fi; }
summary(){ echo ""; echo "########## $PASS passed, $FAIL failed ##########"; }
