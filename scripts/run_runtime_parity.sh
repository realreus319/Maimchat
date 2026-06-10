#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

flag_enabled() {
  local value="${1:-}"
  case "${value,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

OLD_BACKEND_DIR="${MAIMCHAT_OLD_BACKEND_DIR:-/home/tcmofashi/chatbot/l2d_backend}"
OLD_BACKEND_PYTHON="${MAIMCHAT_OLD_BACKEND_PYTHON:-python}"
OLD_BACKEND_PYTEST_ARGS="${MAIMCHAT_OLD_BACKEND_PYTEST_ARGS:-tests/e2e/chat_v1/test_llm_multiturn_e2e.py -v -s}"

echo "[runtime-parity] Running deterministic Maimchat local provider parity tests"
./gradlew :app:testDebugUnitTest \
  --tests com.l2dchat.core.LocalRuntimeFactoryTest \
  --no-daemon

if flag_enabled "${MAIMCHAT_REAL_PROVIDER_PARITY:-0}"; then
  echo "[runtime-parity] Running opt-in Maimchat real-provider parity test"
  ./gradlew :app:testDebugUnitTest \
    --tests com.l2dchat.core.RealProviderRuntimeParityTest \
    --no-daemon
else
  echo "[runtime-parity] Skipping real-provider parity; set MAIMCHAT_REAL_PROVIDER_PARITY=1 to enable"
fi

if flag_enabled "${MAIMCHAT_OLD_BACKEND_PARITY:-0}"; then
  if [[ ! -d "$OLD_BACKEND_DIR" ]]; then
    echo "[runtime-parity] Old backend directory not found: $OLD_BACKEND_DIR" >&2
    exit 2
  fi

  echo "[runtime-parity] Running opt-in old backend parity pytest in $OLD_BACKEND_DIR"
  read -r -a pytest_args <<< "$OLD_BACKEND_PYTEST_ARGS"
  (
    cd "$OLD_BACKEND_DIR"
    "$OLD_BACKEND_PYTHON" -m pytest "${pytest_args[@]}"
  )
else
  echo "[runtime-parity] Skipping old backend parity; set MAIMCHAT_OLD_BACKEND_PARITY=1 to enable"
fi
