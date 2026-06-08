# Deployment, drain, fleet management

How frameforge boxes get installed, updated, and shut down. Locked decisions from Topic 8 (2026-06-04).

---

## Locked decisions

| # | Topic | Decision |
| --- | --- | --- |
| 1 | Drain on SIGTERM | Wait for next chunk boundary, then exit cleanly |
| 2 | Drain on SIGINT | Immediate hard drain (finalize partial, exit) |
| 3 | Update strategy | Pre-load + swap at chunk boundary (zero-gap) |
| 4 | Container approach | Docker + Compose for **MS-01 only**; **Jetson stays systemd-native** (disk constraint) |
| 5 | Image distribution | **GitHub Container Registry** (`ghcr.io/talmolab/frameforge`) |
| 6 | Per-host config | **Env-var overrides per host** via `/etc/frameforge/.env`; image is identical across the fleet |
| 7 | Remote access | **Plain SSH on the office LAN** (single-site for now); Tailscale future-option |
| 8 | Fleet scope | **5 boxes per recording room** target; 1 box (Jetson + MS-01) for testing; multi-site possible, not planned |

---

## Drain semantics — two signals

Two distinct signals so the operator can pick the right shutdown mode:

| Signal | Trigger | Encoder behavior | Use case |
| --- | --- | --- | --- |
| **SIGTERM** | `systemctl stop frameforge` | Soft drain — keeps recording current chunk until frame-count or chunk-index boundary fires, then exits cleanly | Routine updates, planned shutdown |
| **SIGINT** | `systemctl kill -s INT frameforge` | Hard drain — breaks out of `_record_chunk` immediately, finalizes partial `.mp4`, exits | Emergency, something is wrong, accept the partial |

**Worker behavior under drain**:

| Worker | On SIGTERM (soft) | On SIGINT (hard) | Why |
| --- | --- | --- | --- |
| `acq:cam_XX` | Keeps producing frames | Exits ASAP | Encoder needs frames to finish its chunk on soft drain |
| `enc:cam_XX` | Finishes current chunk, exits between chunks | Finalizes partial, exits | The core of the boundary-wait semantic |
| `bcast:cam_XX` | Exits promptly | Exits promptly | Drop-tolerant by design; no buffer worth preserving |
| `transfer` | Exits promptly | Exits promptly | In-flight upload aborts; file re-uploaded on next start |
| `metrics` / `host_sampler` | Exits promptly | Exits promptly | Stateless |

**Why acq has to ignore soft drain**: encoder needs a continuous frame stream to reach 180k or the hour boundary. If acq stopped producing on SIGTERM, the encoder would hang waiting for frames. Soft drain only works if acq keeps running until the encoder is done.

**Acq's exit path on soft drain** (since it doesn't honor the soft drain event):
1. Supervisor's `_shutdown` phase 1 joins encoders (long timeout)
2. Once encoders exit, phase 2 calls `process.terminate()` on remaining workers
3. `process.terminate()` sends SIGTERM to the acq process
4. Python's default SIGTERM handler raises SystemExit
5. Acq's `run()` `finally:` block runs `_safe_close()` on the camera (cleanup)
6. Process exits
7. If acq is mid-`RetrieveResult(timeout=3000)` when SIGTERM arrives, exit may take up to 3 s; supervisor's join-with-timeout then SIGKILLs after 5 s if needed

No hang risk — supervisor explicitly forces termination if acq doesn't exit cleanly.

**systemd unit**:
- `KillSignal=SIGTERM` (default; sends SIGTERM to supervisor)
- `KillMode=mixed` (signal goes only to supervisor's main process; workers receive drain events, not signals)
- `TimeoutStopSec=3700` (1h + buffer; systemd waits this long for clean exit before SIGKILL)

**Operator UX**:
- `systemctl stop frameforge` — soft drain, takes up to ~1h to return
- `systemctl kill -s INT frameforge` — hard drain, returns in seconds
- `systemctl kill -s KILL frameforge` — emergency, no cleanup

---

## Update flow — pre-load + boundary swap

Goal: minimize recording gap. **Honest expectation: ~5–10s typical, up to ~30s worst case** — not zero. The chunk that was being recorded when the operator triggered the update finalizes cleanly (no loss there); the *next* chunk's beginning is short by however long the container takes to recreate + pylon takes to open the camera.

| Phase | Duration | What's happening |
| --- | --- | --- |
| Old container receives SIGTERM | 0 | Soft drain begins |
| Encoder waits for chunk boundary | up to ~1h | Recording continues normally |
| Encoder closes chunk + rename `.part → .mp4` | <500 ms | Current chunk finalized cleanly, no loss |
| Old container exits | <1 s | All workers exited |
| Docker Compose recreates container | 2–5 s | Image already pulled (`docker compose pull`); just bring-up |
| New acq opens camera | 1–3 s | pylon `Open()` + GigE handshake |
| New acq enters grab loop | — | Recording resumes; first chunk starts mid-stream by 5–10 s |

**Net loss**: 250–1500 frames missing from the *start* of the post-update chunk (file is ~10 s shorter than 60 min). Acceptable for routine updates.

**Routine update on a single box** (operator runs from a workstation):
```bash
ssh <box>
cd /opt/frameforge                          # docker-compose.yml lives here on MS-01
docker compose pull                         # pre-loads new image; old container still running
docker compose up -d                        # triggers recreate; sends SIGTERM to old
                                            # old waits for chunk boundary, exits
                                            # new starts with already-pulled image
```

Total recording gap: time between old exit and new start ≈ a few seconds (image already pulled; Compose recreate is fast).

**Wait period**: between `docker compose up -d` and old container exit, the operator waits up to ~1h for the chunk boundary. Run async (`tmux` or `nohup`) if the wait is undesirable.

**Fleet update** (5 boxes per room): manual loop or simple Ansible-style script.
```bash
for host in box-{1..5}; do
    ssh $host "cd /opt/frameforge && docker compose pull && docker compose up -d" &
done
wait
```

Each box does its own boundary wait independently. All boxes finish their chunks naturally; new versions roll out as they become available.

**Hard-stop update** (when soft is impractical, e.g. emergency security patch):
```bash
ssh <box>
systemctl kill -s INT frameforge            # hard drain, partial finalized
cd /opt/frameforge
docker compose pull
docker compose up -d                        # new version, started fresh
```

---

## Container approach — MS-01 only

**MS-01 (production)**: Docker + Compose.
- `Dockerfile` builds frameforge image (Python + pypylon + cv2 + GStreamer)
- `docker-compose.yml` brings up frameforge + MediaMTX as services
- env-var overrides per host via `env_file: /etc/frameforge/.env`
- Volume mounts: `scratch/`, config files, `/dev/shm/` for metrics
- Network: `network_mode: host` (simpler than bridge for camera + RTSP + Prometheus ports)
- GPU: `--device=/dev/dri/renderD128` for Intel iGPU (QSV backend)
- Restart policy: `restart: unless-stopped`

**Jetson (dev/perf platform)**: systemd-native, no Docker.

Rationale: JetPack 4.6 disk is constrained (~16 GB root partition on J1010); Docker images + layers eat into that fast. NVIDIA's container runtime is also fiddly on L4T. Native install via `apt`/`pip` is simpler and proven (we already do it).

**Same code, different deploy**: the frameforge codebase doesn't change between platforms. Deploy plumbing differs.

---

## Image distribution — GitHub Container Registry

Images live at `ghcr.io/talmolab/frameforge:<tag>`.

**Tag scheme**:
- `latest` — most recent main-branch build (automatic via GitHub Actions on push)
- `vX.Y.Z` — tagged releases
- `<git-sha>` — every CI build (rollback granularity)

**Authentication**:
- Public repo + public image: no auth needed for pull
- Private: each MS-01 has a GitHub Personal Access Token (`read:packages` scope) in `/etc/frameforge/.docker_auth`; `docker login ghcr.io` once at provisioning time

**Push flow** (GitHub Action):
```yaml
# .github/workflows/build-image.yml
on:
  push:
    branches: [main]
    tags: ['v*']
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v5
        with:
          push: true
          tags: |
            ghcr.io/talmolab/frameforge:latest
            ghcr.io/talmolab/frameforge:${{ github.sha }}
```

(Workflow not committed yet; captured here for the implementation pass.)

---

## Per-host configuration

Single image across the fleet. Per-host differences via env-var overrides.

**On each MS-01** (`/etc/frameforge/.env`):
```bash
FF_PROFILE=prod
FF_HOST_ID=recording-room-2-box-3       # for session_name suffix, logging tags
VAST_USER=svc_frameforge
VAST_PASS=<from a vault>
```

**Cameras self-identify** — no per-host serial mapping. Each camera's `DeviceUserID` is set once at provisioning time via Basler's Pylon IP Configurator (or `pylonipconfigurator` CLI) to match the abstract id in config: `cam_01`, `cam_02`, …, `cam_08`. Frameforge's acquisition opens cameras by `DeviceUserID`. Benefits:
- `config/prod.yaml` is identical across every box in the fleet
- Swapping a broken camera is plug-and-play: provision its `DeviceUserID` to `cam_03`, slot it in, done
- No per-host config drift; no per-host serial inventory to maintain

`CameraCfg` keeps the optional `serial:` field as a legacy fallback for cameras without `DeviceUserID` set. Single-camera testing uses `CreateFirstDevice` (no ID needed) — current Jetson behavior unchanged.

**Compose file** references the env-file:
```yaml
services:
  frameforge:
    image: ghcr.io/talmolab/frameforge:latest
    env_file: /etc/frameforge/.env
    ...
```

**What lives in the image**:
- All Python code
- `config/prod.yaml` with defaults (camera count, encoder backend choice, bitrate, broadcast settings)

**What lives on the host**:
- The `.env` file (per-host secrets + IDs)
- Scratch partition mount
- systemd unit (Docker Compose service)

---

## Remote access — SSH on LAN

Current single-site deployment uses plain SSH:
- Each MS-01 gets a DHCP-assigned IP on the office LAN; DHCP reservation later if hostnames become important
- Operators SSH from their workstation: `ssh charlie@<ip-or-hostname>`
- Password or key auth — both fine. Disable root login (standard hygiene). Nothing else baked in.

**No Tailscale today.** Reserved for future multi-site or remote-team scenarios. Don't over-engineer.

**Not in scope for the basics**: fail2ban, `ufw`, hardened sshd config. Add when there's a real threat model.

---

## Fleet management

**Scale**: 5 boxes per recording room, single site (Salk basement). 1 testing box current.

**Tooling**: simple shell scripts + SSH for routine ops. Examples:
- `scripts/deploy.sh` — loops over inventory, pulls + restarts
- `scripts/status.sh` — `systemctl status` + `curl :9100/metrics` aggregation
- `scripts/logs.sh` — `journalctl -fu frameforge` parallel tail

No Ansible / Salt / orchestration platform yet. Revisit at 10+ boxes or multi-site.

**Inventory format** (planned): single text file with hostnames, one per line.

**Future-proofing**: design boxes so any orchestration tool can be added later. Don't bake assumptions about box identity into the code; pull host ID from env (`FF_HOST_ID`).

---

## MS-01 day-1 setup (automation plan)

Goal: bring a fresh MS-01 from "out of box" to "recording" with minimal manual steps.

Phase 1 — bootstrap (one-shot, manual):
1. Install Ubuntu 24.04 LTS
2. SSH setup, hostname, static IP
3. Install Docker + Compose
4. Pull `ghcr.io/talmolab/frameforge:latest`
5. Write `/etc/frameforge/.env` with per-host values
6. Place `docker-compose.yml` at `/opt/frameforge/`
7. Enable + start the Compose unit via systemd

Phase 2 — frameforge runs (automatic):
- Container starts on boot via systemd / Docker restart policy
- Pulls latest image on each restart (or pinned tag)
- All workers spawn

A `bootstrap.sh` script (in repo, not yet written) automates Phase 1. Lives under `deploy/` alongside the systemd unit.

**Kernel tuning** (from bandwidth.md): the `/etc/sysctl.d/99-frameforge.conf` drop-in (rmem_max, netdev_max_backlog, etc.) goes down at bootstrap time.

**Network config**: `/etc/netplan/01-frameforge.yaml` sets MTU 9000 on the SFP+ NIC, DHCP on the 2.5G NIC. Also at bootstrap.

**Time sync**: chrony or systemd-timesyncd to keep local clock close to NTP. Important for `chunk_index` math being predictable.

---

## What gets implemented now (Topic 8 code scope) — APPLIED 2026-06-04

- [x] `Context` gains `hard_drain` event alongside `drain`
- [x] `Supervisor` installs two signal handlers (SIGTERM → soft drain; SIGINT → hard drain)
- [x] `Supervisor._shutdown` phases: wait for encoder workers (long timeout), then terminate the rest
- [x] `Encoder._record_chunk` inner loop honors `hard_drain` (not `drain`) — keeps recording through soft drain
- [x] `Encoder._idle_until_next_chunk` honors both events (exits immediately on either)
- [x] `Acquisition` outer + grab loops use `hard_drain` so frames keep flowing during soft drain
- [x] `Transfer` / `metrics` / `host_sampler` / `broadcast` keep current drain check (exit promptly)
- [x] `frameforge.service`: `KillMode=mixed`, `TimeoutStopSec=3700`
- [x] Encoder periodic soft-drain progress log every 60 s when `drain` is set and `hard_drain` is not: `soft drain pending cam=X frames=N/180000 eta_s=N`
- [x] Metric `enc.<cam>.drain_pending` gauge (0/1) for dashboard visibility

## What's deferred

- Dockerfile + docker-compose.yml + bootstrap.sh — when MS-01 lands
- GitHub Actions image build workflow — when ghcr.io repo is created
- `.env.example` per-host config template — when MS-01 lands
- Tailscale wiring — if/when multi-site materializes
- Ansible / fleet tooling — at 10+ boxes or multi-site
- Service-account VAST creds (currently personal creds for testing) — coworker-pending

---

## Resolved

- **Soft drain visibility**: yes — periodic log line + `enc.<cam>.drain_pending` gauge. Now in the implementation scope above.
- **Build matrix**: amd64 only. Revisit ARM if Jetson Docker becomes relevant later.
- **Auto-update**: operator-initiated only. No polling, no cron auto-update layer. Revisit at fleet-scale ops if it becomes a pain point.
