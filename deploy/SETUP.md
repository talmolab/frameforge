# MS-01 setup

_Last updated: 2026-07-29_

Two scripts handle the install. Both idempotent — safe to re-run.

## 0. Have credentials ready before starting

- **Service user password** — you'll set it interactively during bootstrap-box.sh (used for SSH + sudo). User is `talmolab` unless you export `FF_USER` to both scripts.
- **VAST_USER / VAST_PASS** — for SMB upload; needed before frameforge starts (install-frameforge.sh creates a stub, you edit)
- **Hostname** — pick the rig's name (e.g., `lab-rig01`)
- **Camera-facing NIC name** — find on the box: `ip link` (e.g., `enp1s0`)

## 1. Bootstrap the box (OS-level)

```bash
sudo FF_HOSTNAME=lab-rig01 CAMERA_IFACE=enp1s0 \
     ./deploy/scripts/bootstrap-box.sh --with-broadcast
```

Does: hostname, apt installs (multiverse + ffmpeg + intel-driver + mediamtx + prometheus + grafana + avahi + chrony + ssh + uv), service user (prompts for password), system drop-ins (sysctl + journald), camera NIC profile (192.168.10.1/24, `link-local: [ipv4]` — required so cameras that fell back to 169.254/16 still answer discovery and can be ForceIp'd; never set to `[]`, MTU 9000, jumbo off), headless boot (multi-user target, no network-wait — untested until the next box move).

Drop `--with-broadcast` to skip ffmpeg + intel-driver + mediamtx (broadcast off).

## 2. Per-rig configs

Pick the tenant + edit cameras + edit secrets:

```bash
sudo cp config/tenants/example.yaml /etc/frameforge/tenant.yaml
sudoedit /etc/frameforge/tenant.yaml       # set real smb_server / smb_share / smb_root
sudo cp config/cameras.example.yaml /etc/frameforge/cameras.yaml
sudoedit /etc/frameforge/cameras.yaml      # set real serials
```

Camera IP is derived from its id (`cam_0N → 192.168.10.10N`, e.g. `cam_03 → .103`) and applied via ForceIp on startup — independent of order in the file. List them N-ordered anyway for readability.

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
ssh talmolab@lab-rig01.local             # mDNS access from lab LAN
```

Browser (lab LAN):
- `http://lab-rig01.local:3000` — Grafana (default admin / admin)
- `http://lab-rig01.local:8888` — mediamtx web UI, pick a cam to watch live

## Operations

```bash
journalctl -u frameforge -f                # live tail
journalctl -u frameforge --since='-7d'     # last week
systemctl restart frameforge               # graceful restart: drain current chunk (up to 1h), then start
systemctl stop frameforge                  # soft drain: finish current chunk, then stop (no auto-restart)
sudo systemctl kill -s INT --kill-who=main frameforge   # hard drain: abort chunk now (.part discarded next boot)
```

## Re-runs / updates

Code lives at `/usr/local/lib/frameforge` (FF_HOME). Re-running the installer resets it hard to `origin/$GIT_REF`; local edits there are discarded.

```bash
# Update frameforge code only:
sudo GIT_REF=main ./deploy/scripts/install-frameforge.sh
sudo systemctl restart frameforge

# Re-run bootstrap (idempotent — adds new apt deps, refreshes drop-ins):
sudo FF_HOSTNAME=lab-rig01 CAMERA_IFACE=enp1s0 \
     ./deploy/scripts/bootstrap-box.sh --with-broadcast
```

## Lab-IT one-time setup (out of scope of these scripts)

- **Switch**: jumbo frames optional — currently OFF (packets stay 1500). Enable on the switch + set `jumbo_frames: true` only if dropped/incomplete frames appear.
- **Switch IP**: set in your camera subnet (e.g., `192.168.10.2`) via UniFi mobile app or switch web UI
- **Cameras**: no manual IP setup — frameforge assigns `cam_0N → 192.168.10.10N` via ForceIp on startup. Cameras only need to be reachable on the camera subnet (DHCP or link-local); link-local works only because the NIC profile keeps `link-local: [ipv4]` (see step 1).
- **Cameras .pfs** (optional): tune in Basler Pylon Viewer + save .pfs file if you want non-default exposure/gain. Frameforge applies sensible programmatic defaults if no .pfs supplied.
