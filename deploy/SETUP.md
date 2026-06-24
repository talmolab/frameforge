# MS-01 setup

_Last updated: 2026-06-24_

One-time per rig. Run as root.

## 1. User + dirs

```bash
sudo useradd -r -s /usr/sbin/nologin talmolab
sudo install -d -m 755 -o talmolab -g talmolab /usr/local/lib/frameforge
sudo install -d -m 755 -o talmolab -g talmolab /var/lib/frameforge/scratch
```

Mount NVMe at `/var/lib/frameforge/scratch` via `/etc/fstab`.

## 2. Kernel / network tuning

```bash
sudo cp deploy/sysctl-frameforge.conf /etc/sysctl.d/99-frameforge.conf
sudo sysctl --system
```

Camera-facing NIC (replace `<iface>` with `ip link` output):

```bash
sudo nmcli con add type ethernet ifname <iface> ipv4.method manual \
     ipv4.addresses 192.168.10.1/24 con-name frameforge-cams
```

## 3. journald cap

```bash
sudo cp deploy/journald-frameforge.conf \
        /etc/systemd/journald.conf.d/99-frameforge.conf
sudo systemctl restart systemd-journald
```

## 4. Secrets

```bash
sudo install -d -m 700 /etc/frameforge
sudoedit /etc/frameforge/secrets.env       # chmod 600
```

Contents:

```
VAST_USER=cdracos
VAST_PASS=...
```

## 5. Tenant + cameras

Pick the tenant for this lab (or person/bench):

```bash
sudo cp /usr/local/lib/frameforge/config/tenants/<name>.yaml \
        /etc/frameforge/tenant.yaml
```

Per-rig camera list:

```bash
sudo cp /usr/local/lib/frameforge/config/cameras.example.yaml \
        /etc/frameforge/cameras.yaml
sudoedit /etc/frameforge/cameras.yaml      # set serials
```

Order in the camera list = IP slot (first entry → `192.168.10.101`, second → `.102`, ...).

## 6. RTSP relay (mediamtx)

Frameforge pushes broadcast to `rtsp://127.0.0.1:8554/cam_NN`. Install `mediamtx`
and run it as a systemd service; defaults accept push on 8554 and re-serve to
viewers (RTSP for VLC, HLS/WebRTC for browsers).

## 7. Metrics (Prometheus + Grafana)

Install Prometheus on the box; point its config at frameforge's exporter:

```bash
sudo cp deploy/metrics/prometheus.yml /etc/prometheus/prometheus.yml
sudo systemctl restart prometheus
```

Install Grafana, add Prometheus as a datasource, import dashboards from
`deploy/metrics/grafana/`:

- `per_box.json` — single-rig view
- `fleet.json` — cross-rig aggregation

## 8. frameforge service

```bash
sudo cp deploy/frameforge-msone.service /etc/systemd/system/frameforge.service
sudo systemctl daemon-reload
sudo systemctl enable --now frameforge
```

## Operations

```bash
journalctl -u frameforge -f                # live tail
journalctl -u frameforge --since='-7d'     # last week
systemctl stop frameforge                  # soft drain (finishes current chunk)
systemctl kill -s INT frameforge           # hard drain (finalize partial mp4)
```
