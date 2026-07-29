#!/usr/bin/env bash
# Overlay the Maimchat worker fork (./python_src) into the engine's prebuilt rootfs tarballs.
# Run this BEFORE `./gradlew :engine:assembleDebug` whenever the fork changes.
#
# The rootfs tarballs (engine/src/main/assets/rootfs-{arch}.tar.gz) carry the full Alpine rootfs with
# the worker at opt/worker/python_src. We extract, swap in this fork's python_src, and re-tar — the
# rest of the rootfs is left untouched. python_src is pure Python, so the same source serves every arch.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FORK="$HERE/python_src"
ASSETS="$HERE/../engine/src/main/assets"

[ -d "$FORK" ] || { echo "FATAL: fork not found: $FORK" >&2; exit 1; }

for arch in arm64 x86_64; do
    tarball="$ASSETS/rootfs-$arch.tar.gz"
    [ -f "$tarball" ] || { echo "skip $arch: $tarball missing"; continue; }
    echo "== overlaying fork into rootfs-$arch.tar.gz =="
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT

    tar xzf "$tarball" -C "$work"
    dest="$work/opt/worker/python_src"
    [ -d "$dest" ] || { echo "FATAL: $tarball has no opt/worker/python_src" >&2; exit 1; }

    rm -rf "$dest"
    cp -r "$FORK" "$dest"
    find "$dest" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    find "$dest" -name '*.pyc' -delete 2>/dev/null || true

    # Re-tar from the rootfs root so paths stay "./opt/..." like the original.
    ( cd "$work" && tar czf "$tarball" . )
    echo "   $(find "$dest" -name '*.py' | wc -l) .py files -> $tarball ($(du -h "$tarball" | cut -f1))"

    rm -rf "$work"
    trap - EXIT
done
echo "done. now: ./gradlew :engine:assembleDebug"
