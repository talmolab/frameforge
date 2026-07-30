#!/usr/bin/env bash
# install-frameforge.sh — install or update frameforge after bootstrap-box.sh.
#
# Idempotent: safe to re-run for updates. Pulls code, refreshes venv,
# (re)installs systemd units + monitoring configs. Skips files that already
# exist where appropriate (tenant.yaml, cameras.yaml, secrets.env).
#
# Usage (run as root, from repo root):
#   sudo GIT_REF=main ./deploy/install-frameforge.sh
#
# Env:
#   GIT_REF — branch / tag / commit to deploy (default: main)
#   FF_HOME — install location (default: /usr/local/lib/frameforge)

set -euo pipefail

GIT_REF="${GIT_REF:-main}"
FF_HOME="${FF_HOME:-/usr/local/lib/frameforge}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"

echo "=== install-frameforge.sh ==="
echo "  install dir: $FF_HOME"
echo "  git ref:     $GIT_REF"
echo

# ----- 1. Code: git clone or pull -----
echo "[1/7] Syncing code to $FF_HOME..."
if [ ! -d "$FF_HOME/.git" ]; then
    # Fresh clone — use repo root as source if running from a checkout
    sudo -u talmolab git clone "$REPO_ROOT" "$FF_HOME"
fi
cd "$FF_HOME"
sudo -u talmolab git fetch --all --tags
sudo -u talmolab git checkout "$GIT_REF"
sudo -u talmolab git pull --ff-only origin "$GIT_REF" || true

# ----- 2. Python venv via uv -----
# pyproject.toml pins python-preference=only-managed, so uv fetches its own
# interpreter under ~/.local/share/uv/python/. System Python is never linked
# — apt/needrestart can never trigger a frameforge restart from below.
echo "[2/7] Syncing venv via uv..."
sudo -u talmolab bash -c "cd $FF_HOME && /home/talmolab/.local/bin/uv sync"

# ----- 3. Frameforge runtime config (skip if present) -----
echo "[3/7] Frameforge runtime config..."
if [ ! -f /etc/frameforge/tenant.yaml ]; then
    echo "  /etc/frameforge/tenant.yaml not present."
    echo "  Pick a tenant from $FF_HOME/config/tenants/ and copy it:"
    ls "$FF_HOME/config/tenants/"
    echo "  e.g.: sudo cp $FF_HOME/config/tenants/charlie.yaml /etc/frameforge/tenant.yaml"
fi
if [ ! -f /etc/frameforge/cameras.yaml ]; then
    echo "  /etc/frameforge/cameras.yaml not present."
    echo "  Copy template + edit serials:"
    echo "    sudo cp $FF_HOME/config/cameras.example.yaml /etc/frameforge/cameras.yaml"
    echo "    sudoedit /etc/frameforge/cameras.yaml"
fi
if [ ! -f /etc/frameforge/secrets.env ]; then
    cat > /etc/frameforge/secrets.env <<'EOF'
VAST_USER=cdracos
VAST_PASS=changeme
EOF
    chmod 600 /etc/frameforge/secrets.env
    echo "  Created /etc/frameforge/secrets.env (chmod 600). Edit with real creds:"
    echo "    sudoedit /etc/frameforge/secrets.env"
fi

# ----- 4. Systemd units -----
echo "[4/7] Installing systemd units..."
cp "$DEPLOY_DIR/systemd/frameforge.service" /etc/systemd/system/frameforge.service
cp "$DEPLOY_DIR/systemd/heartbeat.service"  /etc/systemd/system/heartbeat.service
cp "$DEPLOY_DIR/systemd/heartbeat.timer"    /etc/systemd/system/heartbeat.timer
cp "$DEPLOY_DIR/scripts/heartbeat.sh"       /usr/local/bin/frameforge-heartbeat.sh
chmod +x /usr/local/bin/frameforge-heartbeat.sh

if [ -f "$DEPLOY_DIR/systemd/mediamtx.service" ]; then
    cp "$DEPLOY_DIR/systemd/mediamtx.service" /etc/systemd/system/mediamtx.service
fi
if [ -f "$DEPLOY_DIR/system/mediamtx.yml" ]; then
    install -d /etc/mediamtx
    cp "$DEPLOY_DIR/system/mediamtx.yml" /etc/mediamtx/mediamtx.yml
fi

# ----- 5. Prometheus config -----
echo "[5/7] Installing prometheus.yml..."
cp "$DEPLOY_DIR/metrics/prometheus.yml" /etc/prometheus/prometheus.yml

# ----- 6. Grafana dashboard provisioning -----
echo "[6/7] Provisioning Grafana dashboard..."
install -d /etc/grafana/provisioning/dashboards
install -d /var/lib/grafana/dashboards
cat > /etc/grafana/provisioning/dashboards/frameforge.yaml <<'EOF'
apiVersion: 1
providers:
  - name: frameforge
    folder: frameforge
    type: file
    options:
      path: /var/lib/grafana/dashboards
EOF
cp "$DEPLOY_DIR/metrics/grafana/per_box.json" /var/lib/grafana/dashboards/

# Prometheus datasource (auto-provisioned)
install -d /etc/grafana/provisioning/datasources
cat > /etc/grafana/provisioning/datasources/prometheus.yaml <<'EOF'
apiVersion: 1
datasources:
  - name: prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
EOF

# Anonymous viewer access so dashboard deep-links open with no login.
# Env drop-in (not grafana.ini) so an apt upgrade can't clobber it.
install -d /etc/systemd/system/grafana-server.service.d
cat > /etc/systemd/system/grafana-server.service.d/10-frameforge-anon.conf <<'EOF'
[Service]
Environment=GF_AUTH_ANONYMOUS_ENABLED=true
Environment=GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
EOF

# ----- 7. Enable + reload + start -----
echo "[7/7] Enabling services..."
systemctl daemon-reload
systemctl enable --now prometheus.service
systemctl enable --now grafana-server.service
systemctl restart grafana-server.service   # apply the anonymous-access drop-in
[ -f /etc/systemd/system/mediamtx.service ] && systemctl enable --now mediamtx.service
systemctl enable heartbeat.timer
systemctl start heartbeat.timer

if systemctl is-active --quiet frameforge.service; then
    echo "  Restarting frameforge..."
    systemctl restart frameforge.service
else
    echo "  frameforge.service installed (not started — start manually after configs verified)."
    echo "    sudo systemctl start frameforge"
fi

echo
echo "=== install-frameforge.sh complete ==="
echo "Verify:"
echo "  systemctl status frameforge          # main service"
echo "  systemctl status heartbeat.timer     # SMB heartbeat"
echo "  journalctl -u frameforge -f          # live tail"
echo "  curl localhost:9100/metrics | head   # frameforge metrics exposed"
echo "  open http://<host>:3000              # Grafana (default admin/admin)"
echo "  open http://<host>:8888              # mediamtx browser viewer (if --with-broadcast)"
