#!/usr/bin/env bash
# install-frameforge.sh — install or update frameforge after bootstrap-box.sh.
#
# Idempotent: safe to re-run for updates. Pulls code, refreshes venv,
# (re)installs systemd units + monitoring configs. Skips files that already
# exist where appropriate (tenant.yaml, cameras.yaml, secrets.env).
#
# Usage (run as root, from repo root):
#   sudo GIT_REF=main ./deploy/scripts/install-frameforge.sh
#
# Env:
#   GIT_REF   — branch / tag / commit to deploy (default: main)
#   FF_HOME   — install location (default: /usr/local/lib/frameforge)
#   FF_USER   — service account that owns and runs frameforge (default: talmolab)
#   FF_EXTRAS — space-separated uv extras to install (default: pylon; e.g. "pylon s3")

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

GIT_REF="${GIT_REF:-main}"
GIT_REMOTE="${GIT_REMOTE:-https://github.com/talmolab/frameforge.git}"
FF_HOME="${FF_HOME:-/usr/local/lib/frameforge}"
FF_USER="${FF_USER:-talmolab}"
FF_EXTRAS="${FF_EXTRAS:-pylon}"

extra_flags=""
for extra in $FF_EXTRAS; do
    extra_flags="$extra_flags --extra $extra"
done

echo "=== install-frameforge.sh ==="
echo "  install dir:  $FF_HOME"
echo "  git ref:      $GIT_REF"
echo "  service user: $FF_USER"
echo "  extras:       $FF_EXTRAS"
echo

# ----- 1. Code: git clone or pull -----
echo "[1/7] Syncing code to $FF_HOME..."
if [ ! -d "$FF_HOME/.git" ]; then
    sudo -u "$FF_USER" git clone "$GIT_REMOTE" "$FF_HOME"
fi
cd "$FF_HOME"
sudo -u "$FF_USER" git fetch -q --all --tags --prune
sudo -u "$FF_USER" git reset -q --hard
if sudo -u "$FF_USER" git rev-parse -q --verify "origin/$GIT_REF" >/dev/null; then
    sudo -u "$FF_USER" git checkout -q -B "$GIT_REF" "origin/$GIT_REF"
else
    sudo -u "$FF_USER" git checkout -q --detach "$GIT_REF"
fi

DEPLOY_DIR="$FF_HOME/deploy"

# ----- 2. Python venv via uv -----
# pyproject.toml pins python-preference=only-managed, so uv fetches its own
# interpreter under ~/.local/share/uv/python/. System Python is never linked
# — apt/needrestart can never trigger a frameforge restart from below.
echo "[2/7] Syncing venv via uv..."
# uv lives in the service user's ~/.local/bin; use it by path so this works from a
# shell that has not re-read its profile since bootstrap installed it.
UV_BIN="$(sudo -u "$FF_USER" -H bash -lc 'command -v uv' 2>/dev/null || true)"
UV_BIN="${UV_BIN:-$(getent passwd "$FF_USER" | cut -d: -f6)/.local/bin/uv}"
[ -x "$UV_BIN" ] || { echo "uv not found for $FF_USER; run bootstrap-box.sh first" >&2; exit 1; }
sudo -u "$FF_USER" -H bash -c "cd '$FF_HOME' && '$UV_BIN' sync $extra_flags"

# ----- 3. Frameforge runtime config (skip if present) -----
echo "[3/7] Frameforge runtime config..."
if [ ! -f /etc/frameforge/tenant.yaml ]; then
    install -m 644 "$FF_HOME/config/tenants/example.yaml" /etc/frameforge/tenant.yaml
    box_tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    if [ -n "$box_tz" ] && ! grep -q '^  timezone:' /etc/frameforge/tenant.yaml; then
        awk -v tz="$box_tz" '{print} /^encode:/{print "  timezone: " tz}' /etc/frameforge/tenant.yaml >/etc/frameforge/tenant.yaml.new
        mv /etc/frameforge/tenant.yaml.new /etc/frameforge/tenant.yaml
    fi
    echo "  Created /etc/frameforge/tenant.yaml from config/tenants/example.yaml (encode.timezone = ${box_tz:-unset})."
    echo "    sudoedit /etc/frameforge/tenant.yaml      # set transfer.storage server/share/root"
fi
if [ ! -f /etc/frameforge/cameras.yaml ]; then
    install -m 644 "$FF_HOME/config/cameras.example.yaml" /etc/frameforge/cameras.yaml
    echo "  Created /etc/frameforge/cameras.yaml from config/cameras.example.yaml; its serials are placeholders."
    echo "    sudoedit /etc/frameforge/cameras.yaml     # one line per real camera, real serials"
fi
if [ ! -f /etc/frameforge/secrets.env ]; then
    cat >/etc/frameforge/secrets.env <<'EOF'
SMB_USER=changeme
SMB_PASS=changeme
EOF
    chmod 600 /etc/frameforge/secrets.env
    echo "  Created /etc/frameforge/secrets.env (chmod 600). Edit with real creds:"
    echo "    sudoedit /etc/frameforge/secrets.env"
fi

# ----- 4. Systemd units -----
echo "[4/7] Installing systemd units..."
sed -e "s/^User=.*/User=$FF_USER/" -e "s/^Group=.*/Group=$FF_USER/" \
    "$DEPLOY_DIR/systemd/frameforge.service" >/etc/systemd/system/frameforge.service
cp "$DEPLOY_DIR/systemd/heartbeat.service" /etc/systemd/system/heartbeat.service
cp "$DEPLOY_DIR/systemd/heartbeat.timer" /etc/systemd/system/heartbeat.timer

if command -v mediamtx >/dev/null; then
    cp "$DEPLOY_DIR/systemd/mediamtx.service" /etc/systemd/system/mediamtx.service
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
cat >/etc/grafana/provisioning/dashboards/frameforge.yaml <<'EOF'
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
cat >/etc/grafana/provisioning/datasources/prometheus.yaml <<'EOF'
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
cat >/etc/systemd/system/grafana-server.service.d/10-frameforge-anon.conf <<'EOF'
[Service]
Environment=GF_AUTH_ANONYMOUS_ENABLED=true
Environment=GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
EOF

# ----- 7. Enable + reload + start -----
echo "[7/7] Enabling services..."
systemctl daemon-reload
systemctl enable --now prometheus.service
systemctl reload-or-restart prometheus.service # pick up prometheus.yml changes
systemctl enable --now grafana-server.service
systemctl restart grafana-server.service # apply the anonymous-access drop-in
if command -v mediamtx >/dev/null; then
    systemctl enable --now mediamtx.service
    systemctl restart mediamtx.service # pick up mediamtx.yml changes
fi
systemctl enable heartbeat.timer
systemctl start heartbeat.timer

# Refuse to start while any config still carries example placeholders.
placeholders=""
grep -q changeme /etc/frameforge/secrets.env && placeholders="$placeholders secrets.env"
grep -q storage.example.org /etc/frameforge/tenant.yaml && placeholders="$placeholders tenant.yaml"
grep -Eq '2345678|2345679' /etc/frameforge/cameras.yaml && placeholders="$placeholders cameras.yaml"

if [ -n "$placeholders" ]; then
    echo "  Not starting frameforge: placeholders left in$placeholders."
    echo "  Edit them (sudoedit /etc/frameforge/<file>), then: sudo systemctl start frameforge heartbeat"
elif systemctl is-active --quiet frameforge.service; then
    echo "  Restarting frameforge..."
    systemctl restart frameforge.service
else
    echo "  Starting frameforge + first heartbeat..."
    systemctl start frameforge.service
    systemctl start --no-block heartbeat.service
fi

echo
echo "=== install-frameforge.sh complete ==="
echo "Verify:"
echo "  systemctl status frameforge          # main service"
echo "  systemctl status heartbeat.timer     # storage heartbeat"
echo "  journalctl -u frameforge -f          # live tail"
echo "  curl localhost:9100/metrics | head   # frameforge metrics exposed"
echo "  open http://<host>:3000              # Grafana (default admin/admin)"
echo "  open http://<host>:8888              # mediamtx browser viewer (if --with-broadcast)"
