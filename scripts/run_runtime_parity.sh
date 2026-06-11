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
