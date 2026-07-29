#!/bin/bash
# Async detached-worker model tests. Run: DEV=emu ./async_tests.sh [a1 a2 a3]
cd "$(dirname "$0")"; DEV=${DEV:-emu} source ./harness.sh

# A1: dispatch is non-blocking — immediate "稍等", result arrives later via completion SYS trigger.
a1() { echo ""; echo "===== A1 async dispatch → delayed delivery ====="
  launch; clear_chat >/dev/null; clear_log
  send "用worker实际运行python算1到100的和并告诉我结果"
  sleep 8; echo "  immediate: $(msgs | tail -1)"
  local imm; imm=$(msgs | grep -c 5050)
  for i in $(seq 1 30); do sleep 6; [ "$(msgs | grep -c 5050)" -ge 1 ] && break; done
  chk "[ $imm -eq 0 ]" "immediate reply is NOT the result (dispatched, not blocked)"
  chk '[ "$(msgs | grep -c 5050)" -ge 1 ]' "result 5050 delivered later via completion trigger"
  msgs | tail -3
}

# A2: while a long worker runs, a new user message sees the background-status block → planner is aware.
a2() { echo ""; echo "===== A2 background-status awareness ====="
  launch; clear_chat >/dev/null; clear_log
  send "用worker实际运行这段python，必须真跑不许跳过sleep：import time; time.sleep(35); print(123*456)"
  for i in $(seq 1 15); do sleep 3; [ "$(worker_n)" -gt 0 ] && break; done
  sleep 3
  send "你现在在忙什么吗"
  for i in $(seq 1 15); do sleep 4; [ "$(turns_done)" -ge 2 ] && break; done
  echo "  reply to '在忙什么': $(msgs | tail -1)"
  chk '[ "$(msgs | tail -1 | grep -ciE "后台|正在|稍等|在跑|处理|任务|计算")" -ge 1 ]' "planner is aware of the running background task"
  for i in $(seq 1 20); do sleep 5; [ "$(msgs | grep -c 56088)" -ge 1 ] && break; done
  chk '[ "$(msgs | grep -c 56088)" -ge 1 ]' "background result 56088 eventually delivered"
}

# A3: two workers dispatched close together — both run (≤3 concurrent) and both deliver.
a3() { echo ""; echo "===== A3 two concurrent workers ====="
  launch; clear_chat >/dev/null; clear_log
  send "用worker运行python算 111+222 等于几，告诉我"
  sleep 4
  send "再用worker运行python算 333+444 等于几，也告诉我"
  for i in $(seq 1 45); do sleep 6; [ "$(msgs | grep -c 333)" -ge 1 ] && [ "$(msgs | grep -c 777)" -ge 1 ] && break; done
  echo "  messages:"; msgs | tail -6
  chk '[ "$(msgs | grep -c 333)" -ge 1 ]' "worker1 result 333 delivered"
  chk '[ "$(msgs | grep -c 777)" -ge 1 ]' "worker2 result 777 delivered"
}

TESTS=("$@"); [ ${#TESTS[@]} -eq 0 ] && TESTS=(a1 a2 a3)
for t in "${TESTS[@]}"; do $t; done
summary
