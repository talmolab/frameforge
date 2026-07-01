# MS-01 setup

_Last updated: 2026-06-30_

Two scripts handle the install. Both idempotent — safe to re-run.

## 0. Have credentials ready before starting

- **talmolab password** — you'll set it interactively during bootstrap-box.sh (used for SSH + sudo)
- **VAST_USER / VAST_PASS** — for SMB upload; needed before frameforge starts (install-frameforge.sh creates a stub, you edit)
- **Hostname** — pick the rig's name (e.g., `talmo-rig01`)
- **Camera-facing NIC name** — find on the box: `ip link` (e.g., `enp1s0`)

## 1. Bootstrap the box (OS-level)

```bash
sudo HOSTNAME=talmo-rig01 CAMERA_IFACE=enp1s0 \
     ./deploy/scripts/bootstrap-box.sh --with-broadcast
```

Does: hostname, apt installs (multiverse + ffmpeg + intel-driver + mediamtx + prometheus + grafana + avahi + chrony + ssh + python3.13 + uv), talmolab user (prompts for password), system drop-ins (sysctl + journald), camera NIC profile (192.168.10.1/24 + jumbo).

Drop `--with-broadcast` to skip ffmpeg + intel-driver + mediamtx (broadcast off).

## 2. Per-rig configs

Pick the tenant + edit cameras + edit secrets:

```bash
sudo cp config/tenants/charlie.yaml /etc/frameforge/tenant.yaml
sudo cp config/cameras.example.yaml /etc/frameforge/cameras.yaml
sudoedit /etc/frameforge/cameras.yaml      # set real serials
```

Order in cameras.yaml = IP slot (first → `192.168.10.101`, second → `.102`, ...).

## 3. Install frameforge

```bash
sudo ./deploy/scripts/install-frameforge.sh
```

Does: git clone/pull, `uv sync` venv, systemd unit installs (frameforge + mediamtx + heartbeat), prometheus.yml, Grafana datasource + dashboard provisioning. Creates `/etc/frameforge/secrets.env` stub if missing.

Edit secrets before starting:

```bash
sudoedit /etc/frameforge/secrets.env       # set real VAST_USER, VAST_PASS
```

## 4. Start

```bash
sudo systemctl start frameforge
```

## Verify

```bash
journalctl -u frameforge -f                # live tail
ssh talmolab@talmo-rig01.local             # mDNS access from lab LAN
```

Browser (lab LAN):
- `http://talmo-rig01.local:3000` — Grafana (default admin / admin)
- `http://talmo-rig01.local:8888` — mediamtx web UI, pick a cam to watch live

## Operations

```bash
journalctl -u frameforge -f                # live tail
journalctl -u frameforge --since='-7d'     # last week
systemctl stop frameforge                  # soft drain (finishes current chunk)
systemctl kill -s INT frameforge           # hard drain (finalize partial mp4)
```

## Re-runs / updates

```bash
# Update frameforge code only:
sudo GIT_REF=main ./deploy/scripts/install-frameforge.sh
sudo systemctl restart frameforge

# Re-run bootstrap (idempotent — adds new apt deps, refreshes drop-ins):
sudo HOSTNAME=talmo-rig01 CAMERA_IFACE=enp1s0 \
     ./deploy/scripts/bootstrap-box.sh --with-broadcast
```

## Lab-IT one-time setup (out of scope of these scripts)

- **Switch**: enable jumbo frames (MTU 9216) on camera-facing ports
- **Switch IP**: set in your camera subnet (e.g., `192.168.10.2`) via UniFi mobile app or switch web UI
- **Cameras**: set static IPs in `192.168.10.101..106` via Basler Pylon IP Configurator (one-time per camera, persistent in EEPROM)
- **Cameras .pfs** (optional): tune in Basler Pylon Viewer + save .pfs file if you want non-default exposure/gain. Frameforge applies sensible programmatic defaults if no .pfs supplied.
