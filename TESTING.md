# Build and GUI Test Flow

## Local build check

```bash
./scripts/check_build.sh
```

This runs:

- `app:assembleDebug`
- `app:testDebugUnitTest`
- `app:assembleDebugAndroidTest`

## Runtime parity checks

```bash
./scripts/run_runtime_parity.sh
```

By default this runs the deterministic local provider parity tests for the
embedded Maimchat runtime. External checks are opt-in:

- `MAIMCHAT_REAL_PROVIDER_PARITY=1` runs
  `RealProviderRuntimeParityTest` against a configured OpenAI-compatible
  endpoint.
- `MAIMCHAT_OLD_BACKEND_PARITY=1` runs the old backend LLM e2e pytest from
  `/home/tcmofashi/chatbot/l2d_backend`.

Real provider example:

```bash
MAIMCHAT_REAL_PROVIDER_PARITY=1 \
MAIMCHAT_REAL_PROVIDER_BASE_URL=https://example.test/v1 \
MAIMCHAT_REAL_PROVIDER_PLANNER_MODEL=model-name \
MAIMCHAT_REAL_PROVIDER_API_KEY=... \
./scripts/run_runtime_parity.sh
```

Old backend example:

```bash
MAIMCHAT_OLD_BACKEND_PARITY=1 ./scripts/run_runtime_parity.sh
```

Useful old backend overrides:

- `MAIMCHAT_OLD_BACKEND_DIR`: defaults to
  `/home/tcmofashi/chatbot/l2d_backend`.
- `MAIMCHAT_OLD_BACKEND_PYTHON`: defaults to `python`.
- `MAIMCHAT_OLD_BACKEND_PYTEST_ARGS`: defaults to
  `tests/e2e/chat_v1/test_llm_multiturn_e2e.py -v -s`.

## GUI instrumentation test

```bash
./scripts/run_gui_tests.sh
```

The script reuses a connected Android device when one is available. If none is connected, it starts the first installed AVD. A specific AVD can be passed as the first argument:

```bash
./scripts/run_gui_tests.sh pixel_6_api_34
```

Useful environment variables:

- `ANDROID_AVD_NAME`: default AVD name when no argument is passed.
- `BOOT_TIMEOUT_SECONDS`: emulator boot wait timeout, default `240`.
- `EMULATOR_HEADLESS`: `auto` by default; use `0` to keep the emulator window when a display is available, or `1` to force headless mode.
- `EMULATOR_EXTRA_ARGS`: extra emulator arguments.

The current GUI smoke test is `MainActivitySmokeTest`, which starts `MainActivity` and verifies that the Compose root is rendered.

## Linux emulator prerequisite

The installed `pixel_6_api_34` AVD uses `x86_64`, so Linux must allow the current user to access `/dev/kvm`. If the GUI script exits with a KVM permission error, add the user to the `kvm` group and start a new login session before rerunning:

```bash
sudo gpasswd -a "$USER" kvm
```
