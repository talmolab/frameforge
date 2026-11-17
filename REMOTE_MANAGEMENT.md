# Remote management: per-rig control and fleet view

Design notes, 2026-10-20. Status: draft, nothing built. This is a starting point, not
a settled design; many decisions are still open (see the end). The first step is
likely a smaller proof of concept: a static `index.html` in the heartbeat folder.
Related: [SYNCED_MODE.md](SYNCED_MODE.md).

## Principles

- **The rig is the source of truth for its own state.** The central server only
  manages and aggregates: it reads state from rigs and passes commands to them. It
  never holds desired state, and it never reconciles rigs toward it.
- **Every rig controls itself through its own HTTP API** in front of its Linux
  services and CLI. Aggregation is a separate layer above the per-rig APIs.
- **Discovery uses the heartbeats.** mDNS never worked on these networks, so
  addresses come from the heartbeat IP, never `.local`.
- **There is always a fallback that doesn't need the central server.**
- **Multi-box synced start is a later goal.** Start single-box; keep the API shaped
  so a two-phase start (arm all boxes, then release the trigger) can be added
  without breaking it.

## Architecture

```
 operator (browser / curl)
        │
        ├──► hcm.talmolab.salk.edu ─► Caddy :443 on the Mac mini
        │        ├── /            hcm dashboard (Svelte)
        │        ├── /api         hcm-core FastAPI :8000
        │        │     └── /api/rigs/*  aggregate + pass-through ──┐
        │        ├── /grafana     Grafana                          │
        │        └── /live/<rig>  proxied mediamtx (WebRTC)        │
        │                                                          │
        └──► fallback: tools/ fleet console, or curl ──────────────┤
                                                                   ▼
   _ff_heartbeat/<host>.json on the share ◄── written by ──  rig: frameforge-api :<port>
   {hostname, ip, timestamp, api_port, version, mode}             ├── systemctl frameforge
          ▲                                                       ├── journalctl
          └── read by the aggregator and the fallback console     ├── config read / validated write
                                                                  └── synced session start / stop / state
```

## Per-rig API: `frameforge-api`

- Its own systemd unit, separate from the `frameforge` recording daemon, so it stays
  up through a recording restart and can be the thing that restarts it.
- Binds to the uplink IP only, never the camera NIC. Every request needs a per-rig
  token.
- Wraps the Linux layer:
  - `systemctl` start, stop, restart, soft drain and hard drain of `frameforge`,
    allowed by a narrow sudoers/polkit rule for exactly those verbs;
  - `journalctl` tails;
  - config read and validated write;
  - disk, NIC and camera status.
- Fronts synced mode: start a session (fps, duration, trigger source), stop it, read
  its state (IDLE / ARM / BARRIER / RUN / STOP / FINALIZE).
- Refuses any command the rig's current state doesn't allow. The rig decides; the
  caller is told why.

## Discovery

`heartbeat.py` already writes `{hostname, ip, timestamp}` to `_ff_heartbeat/` on the
storage root. Extend it with `api_port`, `version` and `mode`. Anything that lists
rigs reads this folder, then asks each rig's API for live state. A heartbeat that is
too old marks the rig as unreachable; it isn't deleted.

## Aggregator: hcm-core FastAPI on the Mac mini

What exists today:

- **Where:** `hcm-core/hcm_core/api/dashboard_api.py` (2,482 lines), one FastAPI
  app. Started by hand with `uvicorn ... --port 8000`; not in `docker-compose.yml`.
  It uses the hcm-core Postgres (`hcm_core.app.db`) and the Prefect client. Its only
  front end is the Svelte dashboard in `hcm-core/dashboard/`.
- **What it does:** about 36 routes, almost all read-only:
  - inference pipeline: overview, throughput, batches (timeline, heatmap, hourly),
    queue status, worker throughput, Sankey flow, health events, logs;
  - coverage and cage status;
  - QC: labels, stats, targets, `cage-qc` time series;
  - media: recording metadata, `.slp`, video with Range support;
  - `/health`.
- **Writes:** priorities (PUT, behind `X-API-Key`) and QC labels (POST, DELETE).
  CORS origins come from env.
- Nothing in it touches a rig today.

What it adds:

- A `/api/rigs/*` router that lists rigs from the heartbeats, fetches live status from
  each rig's API, and passes operator commands through.
- Postgres holds only the audit log (who sent what to which rig, when, and the
  result) and the links from experiment rows to the sessions those commands started.
  It never mirrors rig state.

## Hostname: `hcm.talmolab.salk.edu`

DNS points at an IP, never a port. Ports are routed by a reverse proxy.

1. Give the Mac mini a DHCP reservation or static IP. Keep it wired and set it never
   to sleep.
2. Ask Salk IT for an A record pointing at that IP. Ask first whether they will
   delegate `talmolab.salk.edu` to the lab or only add single records.
3. Run Caddy on the Mac mini on 443, routing paths to `:8000`, Grafana and the
   live-view proxies.
4. TLS: ask IT whether to use their certificate process or Salk's internal CA, and
   whether ACME works for internal names.
5. Firewall: rig uplinks need to reach the Mac mini, and the Mac mini needs to reach
   each rig's API port.

Until then: `http://<mac-ip>:8000`.

## Fallback

When the Mac mini or DNS is down:

- the static fleet console (`tools/`, `npx serve`) reads the same heartbeat folder;
- `curl` with the rig's token reaches any rig's API directly.

## Proof of concept first

A static `index.html` published into `_ff_heartbeat/` next to the heartbeat JSON.
It lists the rigs from the heartbeats and calls each rig's API straight from the
browser. No central server is involved, so it tests the per-rig API and discovery
before any aggregation work.

What to check:

- Opened from the share as `file://`, the page's origin is `null`. The rig APIs need
  CORS for that origin, or the page has to be served instead.
- Browsers may refuse to list the directory from `file://`. The page may need an
  index the heartbeat writer keeps, such as `rigs.json`.
- Where the per-rig token lives in a static page.

## Open decisions

- Per-rig auth: shared token, per-rig token, or mTLS. Who issues and rotates tokens.
- Operator auth on the aggregator: an API key like today's `X-API-Key`, or Salk SSO
  in front of Caddy.
- The API's port, and whether it serves Prometheus `:9100` and the mediamtx `:8889`
  links itself or only points to them.
- Whether the aggregator polls rigs or rigs stream state (SSE or websocket) to
  viewers.
- Heartbeat interval: it decides how quickly a rig's IP change or loss is noticed.
- Which commands are allowed from off-box at all; for example, config writes may
  stay local only.
- Whether to keep the aggregator inside `dashboard_api.py` or split it into its own
  router module or service.
- Multi-box synced start: who owns the shared trigger board, and how the two-phase
  arm-then-release maps onto the per-rig API.
