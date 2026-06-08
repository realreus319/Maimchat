#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/sdk}}"
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"
EMULATOR="${EMULATOR:-$ANDROID_HOME/emulator/emulator}"
AVD_NAME="${ANDROID_AVD_NAME:-${1:-}}"
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-240}"
EMULATOR_HEADLESS="${EMULATOR_HEADLESS:-auto}"
EMULATOR_EXTRA_ARGS="${EMULATOR_EXTRA_ARGS:-}"
LOG_DIR="$ROOT_DIR/build/gui-tests"

mkdir -p "$LOG_DIR"

if [[ ! -x "$ADB" ]]; then
  ADB="$(command -v adb)"
fi

if [[ ! -x "$EMULATOR" ]]; then
  EMULATOR="$(command -v emulator)"
fi

has_ready_device() {
  "$ADB" devices | awk 'NR > 1 && $2 == "device" { found = 1 } END { exit found ? 0 : 1 }'
}

require_kvm_if_needed() {
  local avd_name="$1"
  local avd_config="$HOME/.android/avd/${avd_name}.avd/config.ini"
  local abi=""

  if [[ -f "$avd_config" ]]; then
    abi="$(awk -F= '$1 == "abi.type" { print $2; exit }' "$avd_config" || true)"
  fi

  case "$abi" in
    x86|x86_64)
      if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
        echo "AVD '$avd_name' uses ABI '$abi', but this user cannot access /dev/kvm." >&2
        echo "Add the user to the kvm group, then log out and back in before rerunning this script." >&2
        exit 3
      fi
      ;;
  esac
}

pick_first_avd() {
  "$EMULATOR" -list-avds | sed '/^[[:space:]]*$/d' | head -n 1
}

wait_for_boot() {
  local emulator_pid="${1:-}"
  local emulator_log="${2:-}"
  local deadline=$((SECONDS + BOOT_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    if has_ready_device; then
      break
    fi
    if [[ -n "$emulator_pid" ]] && ! kill -0 "$emulator_pid" 2>/dev/null; then
      echo "Emulator process exited before ADB detected a ready device." >&2
      if [[ -n "$emulator_log" && -f "$emulator_log" ]]; then
        tail -n 80 "$emulator_log" >&2
      fi
      return 1
    fi
    sleep 2
  done

  if ! has_ready_device; then
    echo "No ready Android device appeared within ${BOOT_TIMEOUT_SECONDS}s." >&2
    if [[ -n "$emulator_log" && -f "$emulator_log" ]]; then
      tail -n 80 "$emulator_log" >&2
    fi
    return 1
  fi

  while (( SECONDS < deadline )); do
    local booted
    booted="$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)"
    if [[ "$booted" == "1" ]]; then
      "$ADB" shell input keyevent 82 >/dev/null 2>&1 || true
      return 0
    fi
    sleep 2
  done
  echo "Emulator did not finish booting within ${BOOT_TIMEOUT_SECONDS}s." >&2
  if [[ -n "$emulator_log" && -f "$emulator_log" ]]; then
    tail -n 80 "$emulator_log" >&2
  fi
  return 1
}

"$ADB" start-server >/dev/null

if ! has_ready_device; then
  if [[ -z "$AVD_NAME" ]]; then
    AVD_NAME="$(pick_first_avd)"
  fi

  if [[ -z "$AVD_NAME" ]]; then
    echo "No connected Android device and no AVD found." >&2
    exit 2
  fi

  require_kvm_if_needed "$AVD_NAME"

  emulator_args=(-avd "$AVD_NAME" -no-snapshot -no-boot-anim -gpu swiftshader_indirect)
  if [[ "$EMULATOR_HEADLESS" == "1" || "$EMULATOR_HEADLESS" == "true" ]]; then
    emulator_args+=(-no-window)
  elif [[ "$EMULATOR_HEADLESS" == "auto" && -z "${DISPLAY:-}" ]]; then
    emulator_args+=(-no-window)
  fi

  if [[ -n "$EMULATOR_EXTRA_ARGS" ]]; then
    read -r -a extra_args <<< "$EMULATOR_EXTRA_ARGS"
    emulator_args+=("${extra_args[@]}")
  fi

  log_file="$LOG_DIR/emulator-${AVD_NAME}.log"
  nohup "$EMULATOR" "${emulator_args[@]}" > "$log_file" 2>&1 &
  echo "Started emulator '$AVD_NAME'. Log: $log_file"
  wait_for_boot "$!" "$log_file"
fi

./gradlew --no-daemon app:connectedDebugAndroidTest
