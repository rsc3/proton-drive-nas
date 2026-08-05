#!/bin/sh
# Build Proton's official Drive CLI for CPUs without AVX2.
#
# Proton's prebuilt binary is compiled with Bun's default x64 target, which
# requires AVX2. Most Synology Celerons (J4125 and similar) top out at SSE4.2 and
# die with SIGILL (exit 132) and no error output. Bun ships a "baseline" target
# for exactly this case.
#
# The self-built binary identifies itself as external-drive-sdkclijs@<version>,
# the SDK's own identifier for forks, which Proton's API accepts. Override with
# CLI_APP_VERSION_NAME if you need to.
#
# Output: ./proton-drive-baseline
set -eu

WORK=${WORK:-$HOME/pd-build}
TARGET=${TARGET:-bun-linux-x64-baseline}
CLI_VERSION=${CLI_VERSION:-0.7.0}
JS_VERSION=${JS_VERSION:-0.20.0}
OUT=${OUT:-$PWD/proton-drive-baseline}

mkdir -p "$WORK"
cd "$WORK"

# --- bun ---------------------------------------------------------------------
# Needs >= 1.3.14. Installed locally to keep this self-contained.
export BUN_INSTALL="$WORK/bun"
export PATH="$BUN_INSTALL/bin:$PATH"
if ! command -v bun >/dev/null 2>&1; then
    echo "installing bun into $BUN_INSTALL"
    curl -fsSL https://bun.sh/install | bash > "$WORK/bun-install.log" 2>&1
fi
echo "bun $(bun --version)"

# --- source ------------------------------------------------------------------
if [ ! -d "$WORK/sdk" ]; then
    echo "cloning ProtonDriveApps/sdk"
    git clone --depth 1 https://github.com/ProtonDriveApps/sdk.git "$WORK/sdk" \
        > "$WORK/clone.log" 2>&1
fi

# There is no root workspace: each package must be installed separately, or the
# build fails on unresolved imports (ttag, @noble/hashes, ky) one at a time.
for pj in $(find "$WORK/sdk" -name package.json -not -path "*/node_modules/*"); do
    d=$(dirname "$pj")
    printf 'bun install in %s ... ' "${d#$WORK/sdk/}"
    (cd "$d" && bun install > /dev/null 2>&1) && echo ok || { echo FAILED; exit 1; }
done

# --- build -------------------------------------------------------------------
# Versions are pinned because a shallow clone has no git tags, so the build's
# version detection would otherwise produce 0.0.0.
cd "$WORK/sdk/cli"
echo "building target=$TARGET"
CLI_VERSION="$CLI_VERSION" JS_VERSION="$JS_VERSION" \
    bun run build "$TARGET"

BUILT=$(find release -type f -name proton-drive | head -1)
[ -n "$BUILT" ] || { echo "build produced no binary"; exit 1; }

install -m 0755 "$BUILT" "$OUT"
echo
echo "built: $OUT"
sha256sum "$OUT"
"$OUT" --version || true
echo
echo "Put this on the NAS as \$BASE/bin/proton-drive (or stage it as"
echo "proton-drive.new and let the task install it)."
