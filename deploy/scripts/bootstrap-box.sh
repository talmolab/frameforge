#!/usr/bin/env bash
# bootstrap-box.sh — one-time per-rig OS setup.
#
# Idempotent: safe to re-run. Configures the box for frameforge runtime
# (apt deps, system tunables, user/groups, NIC, drop-ins). Does NOT install
# frameforge itself; that's install-frameforge.sh.
#
# Usage (run as root):
#   sudo HOSTNAME=talmo-rig01 CAMERA_IFACE=enp1s0 ./bootstrap-box.sh [--with-broadcast]
#
# Env:
#   HOSTNAME       — rig hostname to set (required)
#   CAMERA_IFACE   — camera-facing NIC name (required, find via `ip link`)
#   WITH_BROADCAST — "true" if --with-broadcast flag passed

set -euo pipefail

WITH_BROADCAST=false
for arg in "$@"; do
    case "$arg" in
        --with-broadcast) WITH_BROADCAST=true ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

: "${HOSTNAME:?HOSTNAME env var required (e.g. talmo-rig01)}"
: "${CAMERA_IFACE:?CAMERA_IFACE env var required (e.g. enp1s0; check 'ip link')}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== bootstrap-box.sh ==="
echo "  hostname:       $HOSTNAME"
echo "  camera iface:   $CAMERA_IFACE"
echo "  with broadcast: $WITH_BROADCAST"
echo

# ----- 1. Hostname -----
echo "[1/9] Setting hostname..."
hostnamectl set-hostname "$HOSTNAME"

# ----- 2. Apt repos + base packages -----
echo "[2/9] Enabling multiverse + apt update..."
add-apt-repository -y multiverse
apt-get update -qq

echo "[3/9] Installing base packages..."
apt-get install -y --no-install-recommends \
    avahi-daemon \
    chrony \
    openssh-server \
    smbclient \
    curl \
    sudo

if [ "$WITH_BROADCAST" = "true" ]; then
    echo "[3b/9] Installing broadcast packages (ffmpeg, intel-driver, oneVPL, mediamtx)..."
    apt-get install -y --no-install-recommends \
        ffmpeg \
        intel-media-va-driver-non-free \
        libvpl2 \
        vainfo \
        intel-gpu-tools
    # mediamtx ships as a binary, not via apt — install from GitHub release
    if ! command -v mediamtx >/dev/null; then
        MEDIAMTX_VERSION="1.9.3"  # pin a known-good version
        curl -L "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
            | tar -xz -C /usr/local/bin/ mediamtx
        chmod +x /usr/local/bin/mediamtx
    fi
fi

# ----- 4. Monitoring stack -----
echo "[4/9] Installing prometheus + grafana..."
apt-get install -y --no-install-recommends prometheus
# Grafana lives in its own apt repo
if [ ! -f /etc/apt/sources.list.d/grafana.list ]; then
    apt-get install -y --no-install-recommends gnupg
    mkdir -p /etc/apt/keyrings/
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
        > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq
fi
apt-get install -y --no-install-recommends grafana

# Pin observability + broadcast deps so unattended-upgrades doesn't restart them and
# blank dashboards / broadcast mid-recording; upgrades become operator-driven.
apt-mark hold prometheus grafana
if [ "$WITH_BROADCAST" = "true" ]; then
    apt-mark hold ffmpeg intel-media-va-driver-non-free libvpl2
fi

# Belt-and-braces: even if a future apt run touches something frameforge links to,
# needrestart won't auto-restart the service.
install -d /etc/needrestart/conf.d
cat > /etc/needrestart/conf.d/frameforge.conf <<'EOF'
$nrconf{override_rc}{qr(^frameforge\.service$)} = 0;
EOF

# ----- 5. talmolab user + groups -----
echo "[5/9] Creating talmolab user..."
if ! id -u talmolab >/dev/null 2>&1; then
    useradd -m -s /bin/bash talmolab
    echo "  Set password for talmolab:"
    passwd talmolab
fi
usermod -aG sudo,video,render talmolab

# ----- 6. System dirs (owned by talmolab) -----
echo "[6/9] Creating system dirs..."
install -d -m 755 -o talmolab -g talmolab /usr/local/lib/frameforge
install -d -m 755 -o talmolab -g talmolab /var/lib/frameforge/scratch
install -d -m 755 -o root -g root /etc/frameforge

# ----- 7. System drop-ins (kernel + journald) -----
echo "[7/9] Installing system drop-ins..."
cp "$DEPLOY_DIR/system/sysctl-frameforge.conf" /etc/sysctl.d/99-frameforge.conf
sysctl --system >/dev/null

install -d /etc/systemd/journald.conf.d
cp "$DEPLOY_DIR/system/journald-frameforge.conf" /etc/systemd/journald.conf.d/99-frameforge.conf
systemctl restart systemd-journald

# ----- 8. Camera-facing NIC (netplan/networkd — Ubuntu Server default) -----
echo "[8/9] Configuring camera NIC ($CAMERA_IFACE) via netplan..."
install -m 600 /dev/null /etc/netplan/99-frameforge-cams.yaml
cat > /etc/netplan/99-frameforge-cams.yaml <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    $CAMERA_IFACE:
      addresses: [192.168.10.1/24]
      mtu: 9000
      dhcp4: false
      dhcp6: false
      link-local: []
      accept-ra: false
EOF
netplan apply

# ----- 9. uv (Python package manager) -----
echo "[9/9] Installing uv..."
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sudo -u talmolab sh
fi

echo
echo "=== bootstrap-box.sh complete ==="
echo "Next: run install-frameforge.sh to deploy the application."
echo
echo "Verify:"
echo "  hostnamectl                           # hostname set"
echo "  systemctl status avahi-daemon         # mDNS up"
echo "  systemctl status ssh                  # ssh accessible"
echo "  ip link show $CAMERA_IFACE            # mtu 9000"
echo "  sysctl net.core.rmem_max              # 33554432"
echo "  timedatectl status                    # NTP synced"
if [ "$WITH_BROADCAST" = "true" ]; then
    echo "  vainfo                                # iGPU + VAAPI visible"
    echo "  mediamtx --version                    # mediamtx installed"
fi
