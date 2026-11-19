#!/usr/bin/env bash
# kernel-autotune-zram.sh — canonical zram implementation for kernel-autotune
# part-of: kernel-autotune
#
# Standalone use:
#   NUM_DEVICES=4 ZRAM_SIZE_MB=8192 ZRAM_ALGO=zstd ./kernel-autotune-zram.sh
#
# kernel-autotune splices everything below the BEGIN-IMPLEMENTATION marker
# into a generated script with the parameters baked in. The marker and the
# IMPL-VERSION line are load-bearing: kernel-autotune only prefers this file
# over its own embedded copy when IMPL-VERSION matches, so a stale copy left by
# an older package can never silently downgrade behaviour.
#
# kernel-autotune-zram.sh:BEGIN-IMPLEMENTATION
# kernel-autotune-zram.sh:IMPL-VERSION=2.4
set -uo pipefail
# NOTE: deliberately not using `set -e` here — this script juggles several
# independent per-device operations and needs to keep going (and report
# accurately) even if one device fails, the same "best-effort, track
# failures" philosophy kernel-autotune itself uses for its own steps.

# systemd units and `su` (without -) don't always have the sbin dirs on PATH.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

NUM_DEVICES="${NUM_DEVICES:-1}"
ZRAM_SIZE_MB="${ZRAM_SIZE_MB:-0}"
ZRAM_ALGO="${ZRAM_ALGO:-lz4}"
ZRAM_STRICT="${ZRAM_STRICT:-0}"
ZRAM_DRY_RUN="${ZRAM_DRY_RUN:-0}"

SELF_NAME="kernel-autotune-zram"

_log() {
    echo "[$SELF_NAME] $*"
}

_warn() {
    echo "[$SELF_NAME] WARNING: $*" >&2
}

_err() {
    echo "[$SELF_NAME] ERROR: $*" >&2
}

_dry() {
    echo "[DRY RUN] $*"
}

# ---- Basic input validation ----
if ! [[ "$NUM_DEVICES" =~ ^[0-9]+$ ]] || [ "$NUM_DEVICES" -lt 1 ]; then
    _err "NUM_DEVICES must be a positive integer (got: $NUM_DEVICES)"
    exit 1
fi
if ! [[ "$ZRAM_SIZE_MB" =~ ^[0-9]+$ ]] || [ "$ZRAM_SIZE_MB" -lt 1 ]; then
    _err "ZRAM_SIZE_MB must be a positive integer (got: $ZRAM_SIZE_MB)"
    exit 1
fi

PER_DEVICE_MB=$(( ZRAM_SIZE_MB / NUM_DEVICES ))
[ "$PER_DEVICE_MB" -lt 32 ] && PER_DEVICE_MB=32
PER_DEVICE_BYTES=$(( PER_DEVICE_MB * 1024 * 1024 ))

# ---- Required userland tools ----
# Missing tools are a distro-packaging problem (minimal containers, Alpine
# without util-linux, ...). Say so plainly instead of failing per device.
if [ "$ZRAM_DRY_RUN" != "1" ]; then
    for _tool in mkswap swapon swapoff; do
        if ! command -v "$_tool" >/dev/null 2>&1; then
            _err "'$_tool' not found in PATH (install util-linux, or busybox swaps applets)"
            exit 1
        fi
    done
fi

# ---- Ensure the zram kernel module is loaded ----
# zram-control only appears once the module is loaded; on most distros the
# module isn't loaded by default until something requests it. On kernels with
# zram built in (some Alpine/embedded/Android-derived configs) the control
# directory is already present and this block is skipped.
if [ ! -d /sys/class/zram-control ]; then
    if [ "$ZRAM_DRY_RUN" = "1" ]; then
        _dry "Would run: modprobe zram"
    else
        if ! command -v modprobe >/dev/null 2>&1; then
            _err "modprobe not found (install kmod) and zram is not built into this kernel."
            exit 1
        fi
        if ! modprobe zram 2>/dev/null; then
            _err "zram kernel module is not available on this system (modprobe zram failed)."
            _err "ZRAM support may be disabled in this kernel (e.g. WSL), the running kernel's"
            _err "modules may have been removed by an upgrade (reboot), or this is a container."
            exit 1
        fi
        # Loading with no num_devices param creates zram0 by default on
        # some kernels; that's fine, hot_add below handles the rest.
    fi
fi

if [ "$ZRAM_DRY_RUN" = "1" ] && [ ! -d /sys/class/zram-control ]; then
    _dry "zram-control sysfs not present in this environment; cannot simulate device creation further."
    _dry "Would create $NUM_DEVICES zram device(s), ${PER_DEVICE_MB}MB each (${ZRAM_SIZE_MB}MB total), algo=$ZRAM_ALGO"
    exit 0
fi

# ---- Algorithm resolution ----
# The kernel only offers the compressors that were compiled in. zstd needs
# kernel >= 4.19, lz4 needs CONFIG_CRYPTO_LZ4, and some trimmed kernels only
# have lzo. Walk a fallback chain and use whatever this kernel really offers,
# so "zstd requested" never silently becomes "nothing configured".
_resolve_algo() {
    # $1 = zram device name (e.g. zram0); echoes the algorithm to use
    local dev="$1" avail want
    avail=" $(tr -d '[]' < "/sys/block/$dev/comp_algorithm" 2>/dev/null | tr '\n' ' ') "
    for want in "$ZRAM_ALGO" zstd lz4 lzo-rle lzo; do
        if [[ "$avail" == *" $want "* ]]; then
            echo "$want"
            return 0
        fi
    done
    echo "$ZRAM_ALGO"
}

# ---- Figure out which zram device numbers are already active as swap ----
# We treat "already correctly configured" as: an active zram swap device
# whose backing disksize matches what we'd set it to and whose compression
# algorithm matches. This mirrors the idempotency check kernel-autotune's
# setup_zram() already does at the aggregate level, but here we check
# per-device so a partial reconfigure (e.g. NUM_DEVICES changed) doesn't
# tear down devices that are still correctly sized.
declare -a EXISTING_ZRAM_SWAP_DEVS=()
while IFS= read -r line; do
    dev="$(awk '{print $1}' <<<"$line")"
    [[ "$dev" == /dev/zram* ]] || continue
    EXISTING_ZRAM_SWAP_DEVS+=("${dev#/dev/}")
done < <(grep '^/dev/zram' /proc/swaps 2>/dev/null || true)

_device_matches() {
    # $1 = zram device name (e.g. zram0)
    local dev="$1"
    local sys="/sys/block/$dev"
    [ -d "$sys" ] || return 1
    local cur_size cur_algo want_algo
    cur_size="$(cat "$sys/disksize" 2>/dev/null || echo 0)"
    # comp_algorithm reports the active algo wrapped in [brackets], e.g. "lzo lz4 [zstd]"
    cur_algo="$(grep -o '\[[a-z0-9_-]*\]' "$sys/comp_algorithm" 2>/dev/null | tr -d '[]')"
    # Compare against what this kernel would actually give us (after fallback),
    # not the raw request — otherwise a zstd->lz4 fallback would look
    # "mismatched" and get torn down and rebuilt on every apply.
    want_algo="$(_resolve_algo "$dev")"
    [ "$cur_size" -eq "$PER_DEVICE_BYTES" ] && [ "$cur_algo" = "$want_algo" ]
}

_device_in_use_elsewhere() {
    # True if the device is mounted or used as a filesystem/other consumer
    # (e.g. zram-backed /tmp from another tool). Swap use is handled separately.
    local dev="$1"
    grep -q "^/dev/$dev " /proc/mounts 2>/dev/null && return 0
    [ -n "$(ls -A "/sys/block/$dev/holders" 2>/dev/null)" ] && return 0
    return 1
}

CONFIGURED_COUNT=0
FAILED_COUNT=0
SKIPPED_COUNT=0

# ---- Bring up exactly NUM_DEVICES devices ----
for i in $(seq 0 $(( NUM_DEVICES - 1 ))); do
    DEV="zram${i}"
    SYS_PATH="/sys/block/$DEV"

    # If this device already exists and is active swap with matching
    # size/algo, leave it alone entirely (idempotent skip for this device).
    if [[ " ${EXISTING_ZRAM_SWAP_DEVS[*]:-} " == *" $DEV "* ]] && _device_matches "$DEV"; then
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    if [ "$ZRAM_DRY_RUN" = "1" ]; then
        _dry "Would configure /dev/$DEV: ${PER_DEVICE_MB}MB, algo=$ZRAM_ALGO"
        CONFIGURED_COUNT=$((CONFIGURED_COUNT + 1))
        continue
    fi

    # If the device is active as swap but mismatched, take it down first.
    if [[ " ${EXISTING_ZRAM_SWAP_DEVS[*]:-} " == *" $DEV "* ]]; then
        swapoff "/dev/$DEV" 2>/dev/null || true
    fi

    # Create the underlying device node if it doesn't exist yet.
    if [ ! -d "$SYS_PATH" ]; then
        if [ -e /sys/class/zram-control/hot_add ]; then
            # hot_add returns the index of the newly created device on
            # stdout; we don't strictly need it since we're iterating by
            # expected index, but reading it lets us detect failure.
            if ! cat /sys/class/zram-control/hot_add >/dev/null 2>&1; then
                _warn "Failed to hot-add a zram device for $DEV (hot_add write failed)"
                FAILED_COUNT=$((FAILED_COUNT + 1))
                [ "$ZRAM_STRICT" = "1" ] && exit 1
                continue
            fi
        else
            _warn "/sys/class/zram-control/hot_add not present; cannot create $DEV"
            FAILED_COUNT=$((FAILED_COUNT + 1))
            [ "$ZRAM_STRICT" = "1" ] && exit 1
            continue
        fi
    fi

    if [ ! -d "$SYS_PATH" ]; then
        _warn "$DEV still not present after hot_add; skipping"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    # Another consumer (zram-backed /tmp, a filesystem, a device-mapper
    # stack) owns this device. Resetting it would either fail or destroy
    # someone else's data — leave it alone and say why.
    if _device_in_use_elsewhere "$DEV"; then
        _warn "$DEV is in use by something other than swap (mounted or held); skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    # Reset the device before reconfiguring — disksize/comp_algorithm can
    # only be set while the device is not in use (0 = idle/uninitialized).
    echo 1 > "$SYS_PATH/reset" 2>/dev/null || true
    if [ "$(cat "$SYS_PATH/disksize" 2>/dev/null || echo 0)" != "0" ]; then
        _warn "$DEV: could not be reset (still initialized/busy); skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    DEV_ALGO="$(_resolve_algo "$DEV")"
    if [ "$DEV_ALGO" != "$ZRAM_ALGO" ]; then
        _warn "$DEV: algorithm '$ZRAM_ALGO' not offered by this kernel, using '$DEV_ALGO'"
    fi
    if ! echo "$DEV_ALGO" > "$SYS_PATH/comp_algorithm" 2>/dev/null; then
        _warn "$DEV: could not set compression algorithm '$DEV_ALGO'; skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    if ! echo "$PER_DEVICE_BYTES" > "$SYS_PATH/disksize" 2>/dev/null; then
        _warn "$DEV: failed to set disksize to ${PER_DEVICE_MB}MB; skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    if ! mkswap "/dev/$DEV" >/dev/null 2>&1; then
        _warn "$DEV: mkswap failed; skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    # Priority 100 puts zram swap well ahead of any disk-backed swap
    # (default priority is much lower), so the kernel prefers it.
    if ! swapon -p 100 "/dev/$DEV" >/dev/null 2>&1; then
        _warn "$DEV: swapon failed; skipping device"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        [ "$ZRAM_STRICT" = "1" ] && exit 1
        continue
    fi

    CONFIGURED_COUNT=$((CONFIGURED_COUNT + 1))
done

# ---- Tear down any extra zram swap devices beyond NUM_DEVICES ----
# (e.g. user lowered NUM_DEVICES via config + regen-config, or an external
# manager such as zram-generator created devices we don't want)
if [ "$ZRAM_DRY_RUN" != "1" ]; then
    for dev in "${EXISTING_ZRAM_SWAP_DEVS[@]:-}"; do
        [ -z "$dev" ] && continue
        idx="${dev#zram}"
        if [[ "$idx" =~ ^[0-9]+$ ]] && [ "$idx" -ge "$NUM_DEVICES" ]; then
            swapoff "/dev/$dev" 2>/dev/null || true
            if [ -e /sys/class/zram-control/hot_remove ]; then
                echo "$idx" > /sys/class/zram-control/hot_remove 2>/dev/null || true
            fi
            _log "Removed extra device /dev/$dev (NUM_DEVICES decreased)"
        fi
    done
fi

_log "Configured: $CONFIGURED_COUNT, skipped (already correct): $SKIPPED_COUNT, failed: $FAILED_COUNT"

if [ "$CONFIGURED_COUNT" -eq 0 ] && [ "$SKIPPED_COUNT" -eq 0 ]; then
    _err "No zram devices were successfully configured"
    exit 1
fi

exit 0
