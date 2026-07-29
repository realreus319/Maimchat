#!/bin/bash
# #5 mid-run injection: while worker A runs a long task, send B — it should be INJECTED into A's live
# session (a follow-up turn), not spawn a new worker. Verify both A and B results come back.
cd "$(dirname "$0")"; DEV=${DEV:-emu} source ./harness.sh
launch; clear_chat >/dev/null; clear_log
$ADB logcat -c 2>/dev/null
# A's answer must NOT appear in the prompt, else the planner pre-echoes it from the code (false pass).
echo "[A] long worker task (sleep 18 then print 7**11, answer not in prompt)"
send "用worker运行这段python并把输出数字原样告诉我：import time; time.sleep(18); print(7**11)"
for i in $(seq 1 20); do sleep 3; [ "$(worker_n)" -gt 0 ] && break; done
sleep 5
# B must be WORKER-worthy (replier can't compute it), else the planner answers directly & never dispatches.
echo "[B] send a worker follow-up WHILE A is running (should inject into A's session)"
send "再用worker运行python算一下 2 的 100 次方是多少，把完整数字告诉我"
echo "waiting for BOTH A(1977326743) and B(2^100=...5376)..."
for i in $(seq 1 45); do sleep 6; [ "$(msgs | grep -c 1977326743)" -ge 1 ] && [ "$(msgs | grep -c 1267650600228229401496703205376)" -ge 1 ] && break; done
echo "=== inject log ==="
$ADB logcat -d 2>/dev/null | grep -iE "injected follow-up|injected into session|dispatched" | tail -4
echo "=== messages ==="; msgs | tail -5
chk '[ "$(msgs | grep -c 1977326743)" -ge 1 ]' "A result (1977326743) delivered"
chk '[ "$(msgs | grep -c 1267650600228229401496703205376)" -ge 1 ]' "B result (2^100) delivered via mid-run injection"
summary
