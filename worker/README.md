# Maimchat worker fork (`python_src`)

This is a **Maimchat-local fork** of the cc_research Claude-Code Python port
(`/home/tcmofashi/proj/cc_research/python_src`). It is the source that gets baked into the engine's
proot/Alpine rootfs (`com.l2dchat.shell`) and run on-device as the `ask_ai_agent` worker.

## Why a fork

Some worker changes are **Maimchat / on-device-China specific** and must NOT pollute upstream
cc_research (which stays a general Claude-Code port). Those live HERE. **General bug fixes still go
to upstream cc_research first**, then get re-synced down into this fork.

- **Maimchat-specific changes → this fork** (`Maimchat/worker/python_src`).
- **General bugs → upstream** (`/home/tcmofashi/proj/cc_research`), then re-vendor into this fork.

## Current deviations from upstream

- `tools/web_fetch.py` — Great-Firewall handling: fast-fail known-blocked hosts (google family /
  googleusercontent / youtube / twitter|x / facebook / instagram / r.jina.ai / duckduckgo) instead
  of blocking on their timeout, and append GFW guidance ("use cn.bing.com / Baidu, stop after a few
  failures") to any network-unreachable error.
- `tools/web_search.py` — switched the search fallback from **DuckDuckGo (GFW-blocked)** to
  **cn.bing.com (Bing China)** with a real-browser UA + a Bing `b_algo` HTML parser.
- `tools/registry.py` — WebFetch/WebSearch tool descriptions now steer the agent to PREFER the
  browser tools (`mcp__browser__*`, the in-app WebView/Playwright-MCP browser) for general web work,
  reserving raw `urllib` WebFetch for explicit direct URLs.

## How the engine consumes this fork

The rootfs is a prebuilt tarball asset: `engine/src/main/assets/rootfs-{arm64,x86_64}.tar.gz`, with
the worker at `opt/worker/python_src`. **Before building the engine APK**, overlay this fork into the
tarballs:

```bash
Maimchat/worker/pack-rootfs.sh        # overlays ./python_src into both rootfs tarballs
./gradlew :engine:assembleDebug       # then build the engine APK
```

## Re-syncing an upstream bug fix

```bash
# pull a general fix from upstream, keeping the local deviations above:
cp /home/tcmofashi/proj/cc_research/python_src/<changed file> python_src/<changed file>
# then re-apply / verify the deviations listed above are still present, and run pack-rootfs.sh
```
