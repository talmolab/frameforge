# frameforge

Multi-camera acquisition, encoding, and recording for home-cage behavioral monitoring.

frameforge runs on an in-lab PC (an MS-01 rig): it captures from GigE cameras, compresses to
H.264, writes hour-aligned MP4 chunks, and transfers them to central storage — with host
back-pressure so recordings never drop a frame, a low-latency live stream for real-time viewing,
and per-box plus fleet-wide monitoring.

## Architecture

A supervisor process spawns per-camera workers connected by shared-memory rings:

```
cameras → acquisition ─┬─ record ring ──── encoder ── hourly .mp4 chunks ── transfer → SMB / VAST
                       └─ broadcast ring ── broadcast ── mediamtx ────────── WebRTC (live view)
         all workers ─────────────────── metrics ── Prometheus ─────────── Grafana
```

- **Recording** (`libx264`, quality-targeted CRF) preserves fidelity for downstream analysis.
- **Broadcast** (hardware QSV, low-latency) is a throwaway stream off its own ring — it never
  costs the recording a frame.
- **Back-pressure**: a full ring drops frames at the host, never mid-encode.
- **Drain**: `SIGTERM` finishes the current chunk then stops; `SIGINT` aborts it immediately.

## Layout

| Path | What |
|---|---|
| `frameforge/` | the pipeline package — `core/` (supervisor, hardware, rings), `media/` (camera, encoder), `workers/`, `metrics/` |
| `deploy/` | box provisioning — `scripts/` (bootstrap + install), `systemd/`, `system/`, `metrics/` (Prometheus + Grafana), `SETUP.md` |
| `tools/` | operator tools — `calibrator.py` (live lens tuning), `index.html` (static fleet console) |
| `config/` | `cameras.example.yaml`, per-lab `tenants/` |

## Setup

See **[deploy/SETUP.md](deploy/SETUP.md)** — two idempotent scripts, then start the service:

```bash
sudo FF_HOSTNAME=talmo-rig01 CAMERA_IFACE=enp1s0 ./deploy/scripts/bootstrap-box.sh   # OS
sudo ./deploy/scripts/install-frameforge.sh                                          # app
sudo systemctl start frameforge
```

Per-rig config lives in `/etc/frameforge/` (`tenant.yaml`, `cameras.yaml`, `secrets.env`).

## Operations

```bash
sudo journalctl -u frameforge -f              # live logs
sudo systemctl restart frameforge             # graceful: finish current chunk, then start
sudo systemctl stop frameforge                # soft drain: finish chunk, then stop
sudo systemctl kill -s SIGINT frameforge      # hard drain: abort chunk now
```

Dashboards: Grafana on `:3000`, per-box metrics on `:9100`, live camera view via mediamtx `:8889`.
Fleet-wide view: serve the static console — `npx serve -p 8080 tools` → `http://localhost:8080` — then choose the beacon folder.
