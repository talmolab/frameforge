#!/usr/bin/env bash
# make-stick.sh — build the frameforge install stick on a Mac.
#
#   sudo ./deploy/scripts/make-stick.sh ~/Downloads/ubuntu-24.04.1-live-server-amd64.iso /dev/disk4
#
# Writes the stock Ubuntu Server ISO to the stick, then adds a small CIDATA partition
# holding deploy/autoinstall/{user-data,meta-data}. The installer finds that label on
# its own; no kernel args, no ISO repack. Everything on the stick is erased.
#
# Needs: Homebrew gptfdisk (`brew install gptfdisk`) for sgdisk.
# Find the stick with `diskutil list`; check the size column before you pick.

set -euo pipefail

ISO="${1:?usage: make-stick.sh <ubuntu-server.iso> </dev/diskN>}"
DEV="${2:?usage: make-stick.sh <ubuntu-server.iso> </dev/diskN>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOINSTALL="$SCRIPT_DIR/../autoinstall"
LABEL=CIDATA

die() { echo "error: $*" >&2; exit 1; }

[ "$(uname)" = Darwin ] || die "this script targets macOS"
[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -f "$ISO" ] || die "ISO not found: $ISO"
[ -f "$AUTOINSTALL/user-data" ] || die "missing $AUTOINSTALL/user-data"
command -v sgdisk >/dev/null || die "sgdisk not found: brew install gptfdisk"
[[ "$DEV" =~ ^/dev/disk[0-9]+$ ]] || die "device must look like /dev/disk4 (whole disk)"
diskutil info "$DEV" | grep -q "Removable Media: *Removable" || die "$DEV is not removable; refusing"

echo "This erases $DEV:"
diskutil info "$DEV" | grep -E "Device / Media Name|Disk Size"
read -r -p "Type the device name again to continue: " confirm
[ "$confirm" = "$DEV" ] || die "aborted"

RDEV="${DEV/disk/rdisk}"
echo "[1/4] Writing ISO (a few minutes)..."
diskutil unmountDisk "$DEV"
dd if="$ISO" of="$RDEV" bs=4m status=progress
sync

echo "[2/4] Adding $LABEL partition..."
sgdisk -e "$DEV" >/dev/null
sgdisk -n 0:0:+64M -t 0:0700 -c "0:$LABEL" "$DEV" >/dev/null
PART_NUM="$(sgdisk -p "$DEV" | awk -v l="$LABEL" '$NF == l {print $1}')"
[ -n "$PART_NUM" ] || die "could not find the new partition"

echo "[3/4] macOS needs to re-read the partition table."
diskutil eject "$DEV" >/dev/null
read -r -p "Unplug the stick, plug it back in, then press Enter: " _
until [ -e "${DEV}s${PART_NUM}" ]; do sleep 1; done
diskutil unmountDisk "$DEV" >/dev/null 2>&1 || true

echo "[4/4] Formatting and copying autoinstall files..."
newfs_msdos -F 16 -v "$LABEL" "${RDEV}s${PART_NUM}" >/dev/null
diskutil mount "${DEV}s${PART_NUM}" >/dev/null
MOUNT="$(diskutil info "${DEV}s${PART_NUM}" | awk -F': *' '/Mount Point/ {print $2}')"
cp "$AUTOINSTALL/user-data" "$AUTOINSTALL/meta-data" "$MOUNT/"
sync
diskutil eject "$DEV" >/dev/null

echo
echo "Stick ready. On the MS-01: cold boot, F7, pick the USB entry, walk away."
echo "It powers off when done. Pull the stick, power on, and setup starts on the console."
