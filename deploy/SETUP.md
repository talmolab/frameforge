# MS-01 setup

_Last updated: 2026-07-29_

Two scripts handle the install. Both idempotent — safe to re-run.

## 0. Have credentials ready before starting

- **Service user password** — you'll set it interactively during bootstrap-box.sh (used for SSH + sudo). User is `talmolab` unless you export `FF_USER` to both scripts.
- **Storage credentials** — `SMB_USER` / `SMB_PASS` for an SMB share, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for S3; needed before frameforge starts (install-frameforge.sh creates a stub, you edit)
- **Hostname** — pick the rig's name (e.g., `lab-rig01`)
- **Camera-facing NIC name** — find on the box: `ip link` (e.g., `enp1s0`)

## 1. Bootstrap the box (OS-level)

```bash
sudo FF_HOSTNAME=lab-rig01 CAMERA_IFACE=enp1s0 FF_TIMEZONE=America/Los_Angeles \
     ./deploy/scripts/bootstrap-box.sh --with-broadcast
```

Does: hostname, apt installs (multiverse + ffmpeg + intel-driver + mediamtx + prometheus + grafana + avahi + chrony + ssh + uv), timezone (`FF_TIMEZONE`, optional; chunk folders follow the box clock unless the tenant sets `encode.timezone`), grows the root LV into the space the Ubuntu Server installer leaves unallocated (no-op on Desktop installs), service user (prompts for password), system drop-ins (sysctl + journald), camera NIC profile (192.168.10.1/24, `link-local: [ipv4]` — required so cameras that fell back to 169.254/16 still answer discovery and can be ForceIp'd; never set to `[]`, MTU 9000, jumbo off), headless boot (multi-user target, no network-wait — untested until the next box move).

Drop `--with-broadcast` to skip ffmpeg + intel-driver + mediamtx (broadcast off).

Also writes a DHCP stanza for the uplink NIC (`UPLINK_IFACE`, default: the NIC holding the default route, else every other `en*` NIC), because switching the renderer to networkd otherwise drops the address a Desktop install got from NetworkManager.

## 2. Install frameforge

```bash
sudo ./deploy/scripts/install-frameforge.sh
```

Does: git clone/pull, `uv sync --extra pylon` venv (pypylon is an optional extra; Basler cameras need it; override with `FF_EXTRAS="pylon s3"` for S3 storage), systemd unit installs (frameforge + mediamtx + heartbeat + ff-serve), prometheus.yml, Grafana datasource + dashboard provisioning. Copies `config/tenants/example.yaml`, `config/cameras.example.yaml`, and a `secrets.env` stub into `/etc/frameforge/` when missing. Refuses to start frameforge while any of the three still carries example placeholders.

## 3. Per-rig configs

```bash
sudoedit /etc/frameforge/tenant.yaml       # transfer.storage: kind smb (server/share/root) or s3 (bucket, optional prefix/endpoint_url/region); encode.timezone
sudoedit /etc/frameforge/cameras.yaml      # one line per real camera, real serials in quotes
sudoedit /etc/frameforge/secrets.env       # real SMB_USER, SMB_PASS (or AWS_* for s3)
```

Camera IP is derived from its id (`cam_0N → 192.168.10.10N`, e.g. `cam_03 → .103`) and applied via ForceIp on startup — independent of order in the file. List them N-ordered anyway for readability.

## 4. Start

```bash
sudo systemctl start frameforge heartbeat
```

Re-running the installer after the edits does the same start. The heartbeat timer then fires two minutes after install and hourly.

## Verify

```bash
journalctl -u frameforge -f                # live tail
ssh talmolab@lab-rig01.local             # mDNS access from lab LAN
```

Browser (lab LAN):
- `http://lab-rig01.local:3000` — Grafana (default admin / admin)
- `http://lab-rig01.local:8888` — mediamtx web UI, pick a cam to watch live
- `<root>/_ff_heartbeat/<hostname>.json` on storage — heartbeat (address, port, token, versions, session); the hub's registry record, and what the fleet console reads
- `http://lab-rig01:8080/api/status` — `ff serve`, the management API; writes need the `X-FF-Token` header with `FF_TOKEN` from secrets.env

## Install stick

One stick installs any box. Build it once on a Mac (`brew install gptfdisk` first):

```bash
sudo ./deploy/scripts/make-stick.sh ~/Downloads/ubuntu-24.04.1-live-server-amd64.iso /dev/diskN
```

`diskutil list` shows the stick; check the size column. The script writes the stock Ubuntu
Server ISO, then adds a `CIDATA` partition with `deploy/autoinstall/user-data`, which the
installer picks up on its own: whole NVMe, `talmolab` user, git, uv, the frameforge checkout at
`/usr/local/lib/frameforge`, `ff` on PATH, NVMe first in boot order, power off when done.

On the MS-01: plug the uplink RJ45 and the stick, cold boot, F7, pick the USB entry for
partition 2, answer `yes` to the autoinstall prompt, walk away. When the box is off, pull the
stick and power on. tty1 logs `talmolab` in, runs the installer (uv, venv, units; the stick
itself does no network work), then `sudo ff setup`; the password question closes the
auto-login. Rebuild the stick only when `user-data` changes; the ISO is reused as is.

## Setup

Two modes, both re-runnable:

```bash
sudo ff setup                # desk: fresh box. hostname, timezone, camera NIC, service password,
                             #       storage server/share/root + credentials (connection is tested),
                             #       then runs bootstrap-box.sh and install-frameforge.sh for you.
                             #       Prints the device id and hub token once.
sudo ff setup --mode rack    # rack: cameras cabled. Lists serials on the switch, asks for the
                             #       cam_XX=serial mapping (default: current file + new serials on
                             #       the next free id), shows the diff, writes cameras.yaml.
                             #       Apply with `ff restart` (chunk boundary) or `ff start`.
```

Desk writes `tenant.yaml` from `config/tenants/example.yaml` with storage and timezone filled,
an empty `cameras.yaml`, and `secrets.env` (SMB creds, `FF_DEVICE_ID`, `FF_TOKEN`). Editing the
files by hand still works; `ff restart` applies them.

## Operations

`ff` is installed on PATH by the installer and runs as root:

```bash
ff status                 # units, cameras (alive from :9100), desired state, config hash
ff logs -f                # live tail (-n 200 for history)
ff restart                # finish current chunk (up to 1h), restart with config on disk
ff stop                   # finish current chunk, then stop
ff kill                   # SIGKILL now; current chunk discarded
ff pause / ff resume      # stop or start, and record the intent in /var/lib/frameforge/desired.json
ff cams                   # cameras on the switch vs cameras.yaml
ff update [--ref main]    # pull + reinstall; restarts if running
ff --json <verb>          # machine-readable
ff serve                  # what ff-serve.service runs: the same verbs over HTTP on :8080
```

Session uptime (`ff status`, `frameforge_session_uptime_seconds`) counts from the first start
under the current session name and survives restarts; it resets only when the name changes.

Equivalent systemctl, if `ff` is unavailable:

```bash
journalctl -u frameforge -f
systemctl restart frameforge
systemctl stop frameforge
sudo systemctl kill -s SIGKILL frameforge
```

## Re-runs / updates

Code lives at `/usr/local/lib/frameforge` (FF_HOME). Re-running the installer resets it hard to `origin/$GIT_REF`; local edits there are discarded. Tenants predating `transfer.storage`: stop frameforge, move `smb_*` under `transfer.storage` (`kind: smb` + `server/share/root`), rename `VAST_*` → `SMB_*` in secrets.env.

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
