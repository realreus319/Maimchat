#!/bin/bash
# Focused T8 recovery test: kill the app mid-worker, confirm the engine finishes + buffers the
# result to disk, then relaunch and confirm the result is recovered + delivered as a reply.
cd "$(dirname "$0")"; DEV=${DEV:-emu} source ./harness.sh
ENG=com.l2dchat.shell
pend() { $ADB shell "run-as $ENG ls files/pending 2>/dev/null" 2>&1 | tr -d '\r' | grep -c json; }

launch; clear_chat >/dev/null; clear_log
echo "[1] send worker task (factorial 10)"
send "用worker实际运行python代码：import math; print(math.factorial(10))，必须真实执行，把stdout原样贴回来，不许心算"
for i in $(seq 1 20); do sleep 4; [ "$(worker_n)" -gt 0 ] && break; done
echo "    worker up: procs=$(worker_n)  engine=$(engine_pid)"
home; sleep 2
echo "[2] am kill com.l2dchat (main + :chat)"
$ADB shell am kill com.l2dchat; sleep 3
echo "    after kill: main=$(main_pid) chat=$(chat_pid) engine=$(engine_pid) worker=$(worker_n)"
echo "[3] wait for engine to finish the task + buffer result to disk..."
for i in $(seq 1 40); do sleep 5; [ "$(pend)" -gt 0 ] && break; done
echo "    buffered result files on engine disk: $(pend)   (expect >=1)"
echo "[4] relaunch app -> :chat startup should recover + deliver"
launch; sleep 4
for i in $(seq 1 20); do sleep 3; [ "$(msgs | grep -c 3628800)" -ge 1 ] && break; done
echo "[5] result:"
msgs | tail -4
echo ""
chk '[ "$(msgs | grep -c 3628800)" -ge 1 ]' "10! = 3628800 recovered + delivered after kill"
chk '[ "$(pend)" -eq 0 ]' "engine buffer drained (acked) after delivery"
summary
