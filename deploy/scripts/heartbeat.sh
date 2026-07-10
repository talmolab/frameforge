#!/usr/bin/env bash
# Write rig heartbeat to VAST so the fleet console can find it.
# Reuses frameforge's smbclient (smbprotocol) library + tenant.yaml config
# so the code path matches how frameforge uploads happen.

set -euo pipefail

SECRETS=/etc/frameforge/secrets.env
[ -f "$SECRETS" ] || { echo "missing $SECRETS" >&2; exit 1; }
set -a; . "$SECRETS"; set +a
: "${VAST_USER:?}"
: "${VAST_PASS:?}"

export HB_HOSTNAME="$(hostnamectl --static)"
export HB_IP="$(ip -4 -o addr show scope global \
                | grep -v '192\.168\.10\.' \
                | awk '{print $4}' | cut -d/ -f1 | head -1)"
export HB_TS="$(date -Iseconds)"

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
