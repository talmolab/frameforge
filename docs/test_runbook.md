# Jetson final-test runbook (Topic 9)

End-to-end validation on Jetson with isolated camera path (PoE injector → built-in NIC) and office traffic on USB3 1G adapter. Validates: process up, metrics, broadcast, hour-boundary rollover, no drops, VAST upload.

**Time budget**: ~15 min hands-on + run-and-leave for the chunk boundary + week-long unattended afterward.

---

## 0. Dongle sanity (first thing — 30 s)

The 1G dongle must be RTL8153 or AX88179 to work on JetPack 4.6:
```bash
lsusb | grep -i -E "realtek|asix"
```
Look for `0bda:8153` (Realtek) or `0b95:1790` (ASIX). **If you see `0bda:8156` — wrong dongle (2.5G); stop here.**

---

## 1. Install MediaMTX (3 min)

ARM64 binary (no apt package):
```bash
cd /tmp
wget https://github.com/bluenviron/mediamtx/releases/download/v1.8.4/mediamtx_v1.8.4_linux_arm64v8.tar.gz
tar xzf mediamtx_v1.8.4_linux_arm64v8.tar.gz
sudo install -m 755 mediamtx /usr/local/bin/
sudo mkdir -p /etc/mediamtx
sudo install -m 644 mediamtx.yml /etc/mediamtx/mediamtx.yml
```

Systemd unit:
```bash
sudo tee /etc/systemd/system/mediamtx.service > /dev/null <<'EOF'
[Unit]
Description=MediaMTX RTSP server
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
sudo systemctl status mediamtx | head -10
```

MediaMTX should be listening on `:8554` (RTSP) and `:8888` (HLS).

---

## 2. Install GStreamer x264 plugin (1 min)

```bash
sudo apt install -y gstreamer1.0-plugins-ugly
gst-inspect-1.0 x264enc | head -3        # confirm element exists
```

---

## 3. Pull latest frameforge + restart (1 min)

```bash
cd ~/Projects/frameforge
git pull
sudo systemctl restart frameforge
```

---

## 4. Smoke checks (5 min)

Logs — every expected worker started:
```bash
journalctl -u frameforge -n 100 | grep -E "started|starting"
```
Expect: `acq:cam_08`, `enc:cam_08`, `bcast:cam_08`, `transfer`, `metrics`, `host_sampler`.

No errors:
```bash
journalctl -u frameforge -p err --since "2 min ago"
```
Should be empty.

Metrics endpoint:
```bash
curl -s localhost:9100/metrics | head -30
curl -s localhost:9100/metrics | grep -E "acq_incomplete|enc_frames|bcast_session_alive|transfer_session_alive"
```
Expected at steady state:
- `acq_incomplete_total{cam="cam_08"}` ≈ 0 with isolation (was ~9% before)
- `enc_frames_total` increasing at ~50/sec
- `bcast_session_alive` = 1
- `transfer_session_alive` = 1

Broadcast via VLC (from your Mac):
```
VLC → Open Network Stream → rtsp://<jetson-ip>:8554/cam_08
```
Or via browser:
```
http://<jetson-ip>:8888/cam_08
```
Should see live feed with `cam_08` overlay + clock burn-in.

---

## 5. Hour-boundary rollover (wait until next top of hour)

While waiting, run the Mac scraper for the metrics CSV:
```bash
# On Mac, in a tmux/screen:
cd ~/Projects/frameforge
uv run --with requests --with prometheus_client bench/scrape.py <jetson-ip>
```

At the top of the hour:
- Encoder log shows `finalized path=...cam_08.NN.mp4 frames=180000/180000`
- New chunk opens: `opened chunk cam=cam_08 index=NN+1 target=180000`
- Transfer scans (every 30 s) and uploads the finalized chunk: `uploaded path=...` (now at DEBUG level — check with `journalctl -u frameforge -p debug`)
- `transfer_uploaded_total` increments

---

## 6. VAST verification (anytime after first upload)

On your Mac:
```bash
# Mount VAST or check via existing pipeline
ls -lh /talmo/cdracos/frameforge_test/<session>/cam_08/<recording_start>/
```
You should see `cam_08.HH.mp4` files appearing as chunks finalize and upload.

---

## 7. Leave running for the week

Confirm before you log off:
- `systemctl is-active frameforge mediamtx` both `active`
- `transfer_free_mb` well above threshold (5000+)
- `acq_incomplete_total` per-cam rate ≈ 0 (the win from isolation)
- Mac scraper running in tmux/screen so you have a week of metrics CSV to analyze next week

Quick re-check from anywhere via SSH:
```bash
ssh charlie@<jetson-ip> 'systemctl is-active frameforge mediamtx && curl -s localhost:9100/metrics | grep -E "(acq_incomplete|transfer_uploaded|transfer_stuck|transfer_free_mb)"'
```

---

## 8. If anything fails

| Symptom | First check |
| --- | --- |
| `frameforge` won't start | `journalctl -u frameforge -n 50 -p err` |
| `bcast_session_alive == 0` | MediaMTX running? `systemctl status mediamtx`. Then `journalctl -u frameforge | grep broadcast` |
| `acq_incomplete_total` high | Camera link physical issue; check `ip link show eth0` and `acA1300` cable |
| `transfer_session_alive == 0` | Office NIC up? `ip -br addr show <dongle-iface>` should show 10.x.x.x. SMB creds: `cat /etc/frameforge/secrets.env` |
| Broadcast VLC fails to connect | `ss -tlnp | grep 8554` shows MediaMTX listening? Firewall on Jetson allowing 8554? |
| GStreamer pipeline error in logs | `gst-inspect-1.0 x264enc` — plugin missing → `apt install gstreamer1.0-plugins-ugly` |

---

## Coming back next week

Pull the metrics CSV from the Mac scraper. Most-useful checks:
- `acq_incomplete_total` slope over the week (target: ~0)
- `transfer_uploaded_total` ≈ chunks expected for the elapsed hours
- `transfer_stuck_total` should be 0
- `enc_writer_failures_total` should be 0
- `proc_rss_bytes` per worker stable (no leak)
- Any soft-drain pending logs in journald (shouldn't be — service shouldn't restart unless something failed)

If the metrics CSV is in your repo, hand it to me and I'll do the analysis.
