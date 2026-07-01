#!/usr/bin/env bash
# Write rig heartbeat to VAST so the fleet console can find it.
# Independent of frameforge (separate systemd unit + timer).
# Reads VAST creds from /etc/frameforge/secrets.env.

set -euo pipefail

SECRETS=/etc/frameforge/secrets.env
[ -f "$SECRETS" ] || { echo "missing $SECRETS"; exit 1; }
# shellcheck disable=SC1090
source "$SECRETS"

: "${VAST_USER:?}"
: "${VAST_PASS:?}"

HOSTNAME=$(hostnamectl --static)
IP=$(ip -4 -o addr show scope global \
        | grep -v "192\.168\.10\." \
        | awk '{print $4}' | cut -d/ -f1 | head -1)
TS=$(date -Iseconds)
VERSION=$(cat /usr/local/lib/frameforge/.version 2>/dev/null || echo "unknown")

PAYLOAD="{\"hostname\":\"$HOSTNAME\",\"ip\":\"$IP\",\"timestamp\":\"$TS\",\"version\":\"$VERSION\"}"

# Adjust SMB target — pulled from tenant.yaml in a real impl; hardcoded here
SMB_SERVER="pool1.vast.salk.edu"
SMB_SHARE="talmo"
SMB_DIR="cdracos/frameforge/_heartbeat"

echo "$PAYLOAD" | smbclient "//${SMB_SERVER}/${SMB_SHARE}" "$VAST_PASS" \
    -U "$VAST_USER" \
    -c "cd ${SMB_DIR}; put - ${HOSTNAME}.json" \
    >/dev/null
