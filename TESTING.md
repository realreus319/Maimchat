# Build and GUI Test Flow

## Local build check

```bash
./scripts/check_build.sh
```

This runs:

- `app:assembleDebug`
- `app:testDebugUnitTest`
- `app:assembleDebugAndroidTest`

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
