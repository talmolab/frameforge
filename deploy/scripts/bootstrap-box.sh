#!/usr/bin/env bash
# bootstrap-box.sh — one-time per-rig OS setup.
#
# Idempotent: safe to re-run. Configures the box for frameforge runtime
# (apt deps, system tunables, user/groups, NIC, drop-ins). Does NOT install
# frameforge itself; that's install-frameforge.sh.
#
# Usage (run as root):
#   sudo FF_HOSTNAME=lab-rig01 CAMERA_IFACE=enp1s0 ./bootstrap-box.sh [--with-broadcast]
#
# Env:
#   FF_HOSTNAME    — rig hostname to set (required; not HOSTNAME, which bash presets)
#   CAMERA_IFACE   — camera-facing NIC name (required, find via `ip link`)
#   FF_USER        — service account that owns and runs frameforge (default: talmolab)
#   WITH_BROADCAST — "true" if --with-broadcast flag passed

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

WITH_BROADCAST=false
for arg in "$@"; do
    case "$arg" in
        --with-broadcast) WITH_BROADCAST=true ;;
        *)
            echo "Unknown arg: $arg"
            exit 1
            ;;
    esac
done

: "${FF_HOSTNAME:?FF_HOSTNAME env var required (e.g. lab-rig01)}"
: "${CAMERA_IFACE:?CAMERA_IFACE env var required (e.g. enp1s0; check 'ip link')}"
FF_USER="${FF_USER:-talmolab}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== bootstrap-box.sh ==="
echo "  hostname:       $FF_HOSTNAME"
echo "  camera iface:   $CAMERA_IFACE"
echo "  service user:   $FF_USER"
echo "  with broadcast: $WITH_BROADCAST"
echo

# ----- 1. Hostname -----
echo "[1/10] Setting hostname..."
hostnamectl set-hostname "$FF_HOSTNAME"

# ----- 2. Apt repos + base packages -----
echo "[2/10] Enabling multiverse + apt update..."
add-apt-repository -y multiverse
apt-get update -qq

echo "[3/10] Installing base packages..."
apt-get install -y --no-install-recommends \
    avahi-daemon \
    chrony \
    openssh-server \
    curl \
    ffmpeg \
    sudo
systemctl enable --now chrony # NTP — chunk timestamps depend on a synced clock

if [ "$WITH_BROADCAST" = "true" ]; then
    echo "[3b/10] Installing broadcast packages (intel-driver, oneVPL, mediamtx)..."
    apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free \
        libvpl2 \
        vainfo \
        intel-gpu-tools
    # mediamtx ships as a binary, not via apt — install pinned release from GitHub
    if ! command -v mediamtx >/dev/null; then
        MEDIAMTX_VERSION="1.9.3"
        # sha256 of the linux_amd64 asset, computed 2026-09-03 (upstream publishes none)
        MEDIAMTX_SHA256="0b885dbfa4ef9c14cd00191c57d90d804255ff50403a28b85ceee7988c535b60"
        tgz="$(mktemp)"
        curl -fSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" -o "$tgz"
        if [ -n "$MEDIAMTX_SHA256" ]; then
            echo "$MEDIAMTX_SHA256  $tgz" | sha256sum -c - || {
                echo "mediamtx checksum mismatch" >&2
                exit 1
            }
        fi
        tar -xzf "$tgz" -C /usr/local/bin/ mediamtx
        rm -f "$tgz"
        chmod +x /usr/local/bin/mediamtx
    fi
fi

# ----- 4. Monitoring stack -----
echo "[4/10] Installing prometheus + grafana..."
apt-get install -y --no-install-recommends prometheus
# Grafana lives in its own apt repo
if [ ! -f /etc/apt/sources.list.d/grafana.list ]; then
    apt-get install -y --no-install-recommends gnupg
    mkdir -p /etc/apt/keyrings/
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
        >/etc/apt/sources.list.d/grafana.list
    apt-get update -qq
fi
apt-get install -y --no-install-recommends grafana

# Pin so unattended-upgrades can't restart them mid-recording (operator-driven upgrades).
apt-mark hold prometheus grafana ffmpeg
if [ "$WITH_BROADCAST" = "true" ]; then
    apt-mark hold intel-media-va-driver-non-free libvpl2
fi

# Stop needrestart from auto-restarting frameforge after apt runs.
install -d /etc/needrestart/conf.d
cat >/etc/needrestart/conf.d/frameforge.conf <<'EOF'
$nrconf{override_rc}{qr(^frameforge\.service$)} = 0;
EOF

# ----- 5. Service user + groups -----
echo "[5/10] Creating $FF_USER user..."
if ! id -u "$FF_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$FF_USER"
    echo "  Set password for $FF_USER:"
    passwd "$FF_USER"
fi
usermod -aG sudo,video,render "$FF_USER"

# ----- 6. System dirs (owned by service user) -----
echo "[6/10] Creating system dirs..."
install -d -m 755 -o "$FF_USER" -g "$FF_USER" /usr/local/lib/frameforge
install -d -m 755 -o "$FF_USER" -g "$FF_USER" /var/lib/frameforge/scratch
install -d -m 755 -o root -g root /etc/frameforge

# ----- 7. System drop-ins (kernel + journald) -----
echo "[7/10] Installing system drop-ins..."
cp "$DEPLOY_DIR/system/sysctl-frameforge.conf" /etc/sysctl.d/99-frameforge.conf
sysctl --system >/dev/null

install -d /etc/systemd/journald.conf.d
cp "$DEPLOY_DIR/system/journald-frameforge.conf" /etc/systemd/journald.conf.d/99-frameforge.conf
systemctl restart systemd-journald

# ----- 8. Camera-facing NIC (netplan/networkd — Ubuntu Server default) -----
echo "[8/10] Configuring camera NIC ($CAMERA_IFACE) via netplan..."
netplan_file=/etc/netplan/99-frameforge-cams.yaml
netplan_new="$(mktemp)"
cat >"$netplan_new" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    $CAMERA_IFACE:
      addresses: [192.168.10.1/24]
      dhcp4: false
      dhcp6: false
      link-local: [ipv4]
      accept-ra: false
EOF
# Apply only on change: netplan apply bounces the NIC and drops every camera.
if ! cmp -s "$netplan_new" "$netplan_file" 2>/dev/null; then
    install -m 600 "$netplan_new" "$netplan_file"
    netplan apply
else
    echo "  camera NIC profile unchanged; skipping netplan apply"
fi
rm -f "$netplan_new"

# ----- 9. uv (Python package manager) -----
echo "[9/10] Installing uv..."
if ! sudo -u "$FF_USER" -H bash -lc 'command -v uv' >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sudo -u "$FF_USER" sh
fi

# ----- 10. Headless boot (no GUI/login) -----
echo "[10/10] Configuring headless boot..."
# Boot to console, not gdm3 — frameforge runs at multi-user.target and frees the GUI's RAM.
systemctl set-default multi-user.target

# Don't block boot on network-online.target — networkd/heartbeat retry on their own.
systemctl disable --now systemd-networkd-wait-online.service 2>/dev/null || true

echo
echo "=== bootstrap-box.sh complete ==="
echo "Next: run install-frameforge.sh to deploy the application."
echo
echo "Verify:"
echo "  hostnamectl                           # hostname set"
echo "  systemctl status avahi-daemon         # mDNS up"
echo "  systemctl status ssh                  # ssh accessible"
echo "  systemctl get-default                 # multi-user.target (no GUI/login)"
echo "  ip link show $CAMERA_IFACE            # camera NIC up"
echo "  sysctl net.core.rmem_max              # 33554432"
echo "  timedatectl status                    # NTP synced"
if [ "$WITH_BROADCAST" = "true" ]; then
    echo "  vainfo                                # iGPU + VAAPI visible"
    echo "  mediamtx --version                    # mediamtx installed"
fi
