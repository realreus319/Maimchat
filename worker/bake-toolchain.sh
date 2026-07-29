#!/usr/bin/env bash
# OPTIONAL: bake extra Alpine packages (e.g. a C toolchain) into a rootfs asset so they're present
# offline, out-of-the-box, without a first-run `apk add` download.
#
# WHY THIS IS OPTIONAL: the worker can already install tools itself at runtime — `apk add gcc
# musl-dev make` works on-device (proot passes --link2symlink so hardlinked toolchain binaries
# install cleanly) and the install PERSISTS across app updates (WorkerRuntime RUNTIME_VERSION-keyed
# extraction). So baking only saves the one-time ~200 MB gcc download; it also makes the APK much
# bigger (full gcc+binutils ≈ 200 MB per arch). Prefer a small set (e.g. `tcc` ≈ 1 MB) if you bake.
#
# HOW IT WORKS: there is no host-side chroot/qemu here — we install INSIDE the engine's own proot on
# a CONNECTED DEVICE of the matching arch (x86_64 emulator, or an arm64 phone), then pull the rootfs
# back and repack the asset. Run once per arch.
#
# Usage:  ./bake-toolchain.sh <arch> "<apk packages>" [adb-serial-args...]
#   ./bake-toolchain.sh x86_64 "tcc"                 -s emulator-5554
#   ./bake-toolchain.sh arm64  "gcc musl-dev make"   -s <device>
set -euo pipefail
ARCH="${1:?arch: x86_64|arm64}"; PKGS="${2:?apk packages, e.g. \"gcc musl-dev make\"}"; shift 2
ADB="${ADB:-adb} $*"
ENG=com.l2dchat.shell
HERE="$(cd "$(dirname "$0")" && pwd)"
ASSET="$HERE/../engine/src/main/assets/rootfs-$ARCH.tar.gz"
[ -f "$ASSET" ] || { echo "FATAL: $ASSET missing" >&2; exit 1; }

echo ">> ensuring rootfs is extracted on device (launch PocActivity once if not)…"
$ADB shell am start -n $ENG/.PocActivity >/dev/null 2>&1 || true; sleep 8

echo ">> apk add ($PKGS) inside the device proot…"
$ADB shell "run-as $ENG sh -c '
cd files
export PROOT_LOADER=\$PWD/bin/loader PROOT_LOADER_32=\$PWD/bin/loader32 PROOT_TMP_DIR=\$PWD/tmp LD_LIBRARY_PATH=\$PWD/bin
bin/proot --kill-on-exit --link2symlink -r rootfs -0 -b /dev -b /proc -b /sys -w /root /bin/sh -c \"export PATH=/usr/bin:/bin:/usr/sbin:/sbin; apk add --no-cache $PKGS\"
'"

echo ">> repacking device rootfs -> $ASSET (this is large; the APK will grow)…"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
$ADB exec-out run-as $ENG tar c -C files rootfs > "$TMP/rootfs.tar"
( cd "$TMP" && mkdir x && tar xf rootfs.tar -C x && cd x && tar czf "$ASSET" . )
echo ">> done. Bump WorkerRuntime.RUNTIME_VERSION, then: ./gradlew :engine:assembleDebug"
