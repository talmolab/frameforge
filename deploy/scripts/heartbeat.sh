#!/usr/bin/env bash
# Write rig heartbeat to VAST so the fleet console can find it.
# Reuses frameforge's smbclient (smbprotocol) library + tenant.yaml config
# so the code path matches how frameforge uploads happen.
#
# Retries for a few minutes: the timer only re-fires hourly, and after a room
# move the uplink can take a while to become routable — without retries a slow
# network settle means no heartbeat (and an unfindable box) for up to an hour.
# Keep the total budget under heartbeat.service's TimeoutStartSec.

set -euo pipefail

SECRETS=/etc/frameforge/secrets.env
[ -f "$SECRETS" ] || {
    echo "missing $SECRETS" >&2
    exit 1
}
set -a
# shellcheck source=/dev/null
. "$SECRETS"
set +a
: "${VAST_USER:?}"
: "${VAST_PASS:?}"

HB_MAX_ATTEMPTS="${HB_MAX_ATTEMPTS:-20}"
HB_RETRY_SECONDS="${HB_RETRY_SECONDS:-15}"

write_heartbeat() {
    local ip
    ip="$(ip -4 -o addr show scope global |
        awk '{print $4}' | cut -d/ -f1 |
        grep -v '^192\.168\.10\.' | head -1 || true)"
    [ -n "$ip" ] || {
        echo "no routable uplink IP yet" >&2
        return 1
    }

    HB_HOSTNAME="$(hostnamectl --static)"
    HB_TS="$(date -Iseconds)"
    export HB_HOSTNAME HB_IP="$ip" HB_TS

    /usr/local/lib/frameforge/.venv/bin/python <<'PY'
import json, os, yaml, smbclient

with open("/etc/frameforge/tenant.yaml") as f:
    tenant = yaml.safe_load(f)
smb_server = tenant["transfer"]["smb_server"]
smb_share  = tenant["transfer"]["smb_share"]
user_ns    = tenant["transfer"]["smb_root"].split("/", 1)[0]

hb_dir  = f"//{smb_server}/{smb_share}/{user_ns}/_ff_heartbeat"
hb_path = f"{hb_dir}/{os.environ['HB_HOSTNAME']}.json"

payload = {
    "hostname":  os.environ["HB_HOSTNAME"],
    "ip":        os.environ["HB_IP"],
    "timestamp": os.environ["HB_TS"],
}

smbclient.register_session(
    smb_server,
    username=os.environ["VAST_USER"],
    password=os.environ["VAST_PASS"],
    port=445,
)
smbclient.makedirs(hb_dir, exist_ok=True)
with smbclient.open_file(hb_path, mode="w") as f:
    f.write(json.dumps(payload) + "\n")
print("wrote:", hb_path)
PY
}

attempt=1
while true; do
    if write_heartbeat; then
        exit 0
    fi
    if [ "$attempt" -ge "$HB_MAX_ATTEMPTS" ]; then
        echo "heartbeat failed after $attempt attempts" >&2
        exit 1
    fi
    echo "heartbeat attempt $attempt failed; retrying in ${HB_RETRY_SECONDS}s" >&2
    attempt=$((attempt + 1))
    sleep "$HB_RETRY_SECONDS"
done
