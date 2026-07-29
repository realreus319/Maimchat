#!/bin/bash
# Maimchat standard E2E suite. Run:  DEV=emu ./tests.sh [t1 t2 ...]   (no args = all fast tests)
# Each test: clear → send → wait → assert from DB + timing log.
cd "$(dirname "$0")"
DEV=${DEV:-emu} source ./harness.sh

setup() { echo ""; echo "===== $1 ====="; launch; clear_chat; clear_log; }

# --- T1: baseline single reply ---
t1() { setup "T1 baseline reply"
  send "你好，请用一句话介绍你自己"
  wait_turn 12 1 || true
  chk '[ "$(asst_count)" -ge 1 ]' "got an assistant reply"
  chk '[ "$(replies_sent)" -ge 1 ]' "timing shows replySent=true"
  chk '[ -z "$(errlog)" ]' "no errors in log"
  msgs | tail -4
}

# --- T2: two-message interrupt (B cancels A's in-flight reply) ---
t2() { setup "T2 two-message interrupt (fork/decision)"
  send "请给我讲一个非常详细的关于宇宙起源的长故事，要分很多段落"
  sleep 2                                   # let A start generating
  send "算了，2加2等于几"                    # B interrupts
  wait_turn 16 1 || true; sleep 6
  chk '[ "$(rounds | grep -ci DECID)" -ge 1 ] || [ "$(timing | grep -ci decision)" -ge 1 ]' "a DECISION turn occurred"
  chk '[ "$(msgs | grep -c "A:")" -ge 1 ]' "B got an answer"
  chk '[ "$(msgs | tail -2 | grep -c "4")" -ge 1 ]' "final reply answers B (=4)"
  chk '[ -z "$(errlog)" ]' "no Unknown tool / errors"
  msgs | tail -5
}

# --- T3: three-message recursive interrupt ---
t3() { setup "T3 recursive 3-message interrupt"
  send "请详细讲讲深海生物的故事，越长越好"
  sleep 2; send "不对，讲讲沙漠"
  sleep 2; send "停，告诉我3乘以3等于几就行"
  wait_turn 20 1 || true; sleep 6
  chk '[ "$(asst_count)" -ge 1 ]' "produced a final reply"
  chk '[ "$(msgs | tail -2 | grep -c "9")" -ge 1 ]' "final reply answers last msg (=9)"
  chk '[ -z "$(errlog)" ]' "no crash/errors"
  msgs | tail -6
}

# --- T4: C-language assignment (worker engine) ---
t4() { setup "T4 C assignment (worker)"
  send "用worker实际编写并gcc编译运行一个C程序：打印前10个斐波那契数。必须真实运行可执行文件，把程序的真实stdout原样贴回来，不许心算或用公式代替"
  wait_worker 90 || echo "   (worker wait timed out)"; sleep 8
  chk '[ "$(asst_count)" -ge 1 ]' "got a reply about the C program"
  chk '[ "$(msgs | grep -ciE "fib|斐波|0.*1.*1.*2.*3")" -ge 1 ]' "reply mentions fibonacci/result"
  chk '[ "$(errlog | grep -ci truncat)" -eq 0 ]' "no max-tokens truncation failure"
  msgs | tail -4; echo "--- timing ---"; timing | tail -8
}

# --- T5: web scraping → submit_file (worker + browser) ---
t5() { setup "T5 web scrape + submit_file"
  send "用浏览器打开 example.com，把页面主标题文字抓取出来，并把抓到的内容存成一个txt文件发给我"
  wait_worker 90 || echo "   (worker wait timed out)"; sleep 8
  chk '[ "$(asst_count)" -ge 1 ]' "got a reply"
  chk '[ "$(has_file)" -ge 1 ] || [ "$(msgs | grep -ciE "example|illustrative|domain")" -ge 1 ]' "delivered file or scraped text"
  msgs | tail -4
}

# --- T6: reply half-way then HOME (background foreground turn) ---
t6() { setup "T6 reply mid-flight → HOME (background)"
  send "请用三段话详细介绍北京的历史"
  sleep 3; home                              # drop to launcher mid-reply
  echo "   (backgrounded; chat process pid=$(chat_pid))"
  wait_turn 16 1 || true; sleep 4
  launch                                     # come back
  chk '[ "$(asst_count)" -ge 1 ]' "reply completed while backgrounded"
  chk '[ "$(replies_sent)" -ge 1 ]' "replySent=true even in background"
  msgs | tail -3
}

# --- T7: worker running then HOME (worker keeps going in background) ---
t7() { setup "T7 worker running → HOME (background)"
  send "用worker实际运行python代码：print(sum(range(1,101)))，必须真实执行，把stdout原样贴回来，不许心算"
  for i in 1 2 3 4 5 6 7 8; do sleep 4; [ "$(worker_n)" -gt 0 ] && break; done
  echo "   worker procs before home: $(worker_n)"; home
  sleep 10; echo "   worker procs after home:  $(worker_n) (engine pid=$(engine_pid))"
  wait_worker 60 || echo "   (worker wait timed out)"; sleep 6; launch
  chk '[ "$(asst_count)" -ge 1 ]' "worker finished + replied in background"
  chk '[ "$(msgs | grep -c 5050)" -ge 1 ]' "result 5050 delivered"
  msgs | tail -3
}

# --- T8: worker running then KILL MAIN process (engine survives) ---
t8() { setup "T8 worker running → kill main app process"
  send "用worker实际运行python代码：import math; print(math.factorial(10))，必须真实执行，把stdout原样贴回来，不许心算"
  for i in 1 2 3 4 5 6 7 8; do sleep 4; [ "$(worker_n)" -gt 0 ] && break; done
  home; sleep 2
  echo "   killing main pid=$(main_pid); engine pid=$(engine_pid)"; kill_main; sleep 3
  chk '[ "$(worker_n)" -gt 0 ] || [ -n "$(engine_pid)" ]' "engine/worker survived main kill"
  wait_worker 60 || echo "   (worker wait timed out)"; sleep 6; launch; sleep 4
  chk '[ "$(asst_count)" -ge 1 ]' "result delivered after main was killed+relaunched"
  chk '[ "$(msgs | grep -c 3628800)" -ge 1 ]' "10! = 3628800 delivered"
  msgs | tail -3
}

TESTS=("$@"); [ ${#TESTS[@]} -eq 0 ] && TESTS=(t1 t2 t3)
for t in "${TESTS[@]}"; do $t; done
summary
