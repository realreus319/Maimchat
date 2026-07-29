#!/bin/bash
# Problem #3: cross-turn worker context. Make a file in turn 1 (don't send), then "把刚才那个文件发给我"
# in turn 2 — needs the compact-background injection (3.1) + worker session hint (3.2) to find it.
cd "$(dirname "$0")"; DEV=${DEV:-emu} source ./harness.sh
bubbles() { _pulldb; sqlite3 "$DBT/maimchat_runtime.db" "SELECT count(*) FROM messages WHERE file_info_json IS NOT NULL;" 2>/dev/null; }

launch; clear_chat >/dev/null; clear_log
echo "[turn1] make /root/sumdemo.py, report path, DON'T send"
send "用worker写一个python脚本保存到 /root/sumdemo.py，内容是打印1到100的和；创建好后把文件路径告诉我就行，这次先不用把文件发给我"
# wait for turn1's ASSISTANT reply (worker completed), not the user echo
for i in $(seq 1 50); do sleep 5; [ "$(asst_count)" -ge 1 ] && break; done
sleep 8
echo "  bubbles after t1: $(bubbles) (expect 0)"
echo "  t1 reply: $(msgs | grep '^A:' | tail -1)"
echo "[turn2] 把刚才那个文件发给我 (cross-turn: needs compact background + session self-lookup)"
send "把刚才那个文件发给我"
for i in $(seq 1 45); do sleep 5; [ "$(bubbles)" -ge 1 ] && break; done
sleep 5
echo "=== final ==="
echo "  bubbles: $(bubbles)"
echo "  newest agent_files: $($ADB shell "run-as com.l2dchat ls -t files/agent_files 2>/dev/null | head -1" 2>&1 | tr -d '\r')"
msgs | tail -2
chk '[ "$(bubbles)" -ge 1 ]' "prior-turn file delivered across turns (compact background + session hint)"
summary
