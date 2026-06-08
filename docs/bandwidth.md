# Bandwidth & data-transfer audit

Complete walkthrough of the camera → disk path, with bandwidth math and the rationale for every config knob. Covers both current Jetson platform and the incoming MS-01 production platform, side-by-side.

**Status (2026-06-01):** MS-01 + Ubiquiti Enterprise 8 PoE switch + 10G SFP+ DAC ordered. Jetson stays as dev/perf platform for single-camera work. Target production = **8 cameras**.

---

## TL;DR — the stack at a glance

| Layer | What carries the data | Headroom check (8 cams MS-01) | Headroom check (1 cam Jetson) |
| --- | --- | --- | --- |
| 1. Camera link (GigE Vision) | UDP / GVSP over copper to camera NIC | 4.16 Gbps total ingress; 10G DAC carries it (~2.4× headroom) | 520 Mbps per camera; 1G NIC carries 1 camera (~50% utilization) |
| 2. Kernel networking | NIC ring buffer → softirq → socket → userspace | rmem_max + jumbo frames cover bursts | shared LAN = packet loss (root cause of our drops) |
| 3. Application | pypylon → shm_ring → mp.Queue → encoder | 256-slot queue holds 5s buffer @ 50fps; ring 128 slots is 2.5s | same; single-cam means most ingress lands in ring directly |
| 4. Upload + storage | NVMe write → SMB to VAST | 1.3 Mbps total; NVMe + Gigabit are massively overcommitted | same, gigabit uplink fine |

**Binding constraint:** layer 1 (camera link). On Jetson today: packet loss from sharing GigE with office traffic. On MS-01: should be near-zero with isolated switch + 10G DAC + jumbo frames.

---

## Layer 1 — Camera link (GigE Vision)

### What's on the wire

Basler `acA1300-75gm`: 1280 × 1024 × 8-bit mono = **1,310,720 bytes per frame** (~1.3 MB). At 50 fps:
- **Per-camera raw bandwidth = 1.3 MB × 50 = 65 MB/s = 520 Mbps**
- 8 cameras = **4.16 Gbps** aggregate camera ingress

The camera doesn't send a 1.3 MB blob in one go. The **GVSP** protocol (GigE Vision Streaming Protocol) chops each frame into UDP packets sized by `GevSCPSPacketSize`. Each packet has GVSP + UDP + IP + Ethernet headers (~50 bytes overhead).

### Packet count per frame (MTU matters)

| MTU | GevSCPSPacketSize (payload) | Packets per frame | Packets/sec per camera | Packets/sec, 8 cameras |
| --- | --- | --- | --- | --- |
| 1500 (default) | 1500 − 50 = 1450 useful | 1,310,720 / 1450 ≈ **904** | 904 × 50 = **45,200** | 361,600 |
| 9000 (jumbo) | 9000 − 50 = 8950 useful | 1,310,720 / 8950 ≈ **147** | 147 × 50 = **7,350** | 58,800 |

Jumbo frames cut packet count by **~6×**. Fewer packets means:
- Lower CPU interrupt rate
- Lower probability that any single packet is dropped (one lost packet → entire frame marked `incomplete` by pylon)
- Less switch buffer pressure (especially when 8 cameras burst simultaneously)

**Requirement for jumbo frames**: MTU 9000 must be set on every hop — camera, switch, NIC. If any link is 1500, packets either fragment (bad) or get silently dropped.

### What "incomplete" actually means

pypylon's `result.GrabSucceeded() == False` (the [`acq.<cam>.incomplete`](../frameforge/acquisition.py) counter) fires when GVSP doesn't deliver every packet for a frame before the timeout. Pylon then has a partial frame, marks it bad, and we drop the whole frame. **One missing UDP packet anywhere in the path = one whole 1.3 MB frame lost.** This is why GigE Vision is sensitive to even tiny packet loss rates.

### GevSCPD — inter-packet delay

`GevSCPD` (Stream Channel Packet Delay) is how long the camera waits between sending consecutive packets, in GigE timestamp ticks (usually ns).

| Scenario | Recommended value | Why |
| --- | --- | --- |
| Single camera on dedicated NIC | `0` | No contention; line-rate fine |
| Multiple cameras on shared switch | `~3000–5000` (ticks) | Spaces out simultaneous packet bursts from multiple cameras so switch buffer can drain |

**Jetson today:** 1 camera, but on shared office LAN. GevSCPD=0 doesn't help because the contention isn't another camera — it's office traffic. The fix is isolation, not GevSCPD.

**MS-01 with 8 cameras + isolated switch:** GevSCPD should be set to a small positive value (3000–5000 ticks) to stagger 8 simultaneous frame bursts. Worth measuring with `acq.incomplete` after deployment.

### Code references

| Variable | File | Default | MS-01 target |
| --- | --- | --- | --- |
| `acq.packet_size` → `GevSCPSPacketSize` | [config.py:35](../frameforge/config.py#L35), [acquisition.py:96](../frameforge/acquisition.py#L96) | 1500 | **8192** (assumes MTU 9000) |
| `acq.inter_packet_delay_ns` → `GevSCPD` | [config.py:36](../frameforge/config.py#L36), [acquisition.py:97](../frameforge/acquisition.py#L97) | 0 | **0** initially; tune to 3000–5000 if multi-cam drops appear |
| `acq.max_num_buffer` → pylon grab pool | [config.py:37](../frameforge/config.py#L37), [acquisition.py:99](../frameforge/acquisition.py#L99) | 64 | **64** (each buffer = 1.3 MB, 8 cams × 64 × 1.3 MB ≈ 670 MB — fine on MS-01 RAM) |
| `acq.retrieve_timeout_ms` → pylon RetrieveResult timeout | [config.py:38](../frameforge/config.py#L38), [acquisition.py:103](../frameforge/acquisition.py#L103) | 0 (uses GevHeartbeatTimeout) | **3000** explicit; gives 60 frame periods of slack |

---

## Layer 2 — Kernel networking

### Where packets buffer

```
Camera → cable → NIC PHY → NIC RX ring buffer (in NIC RAM)
                              ↓ DMA + IRQ
                          kernel socket receive buffer (rmem)
                              ↓ recvfrom()
                          pypylon (in our acquisition process)
```

Two distinct buffers can drop packets here:
1. **NIC RX ring** (hardware buffer on the network card). Fixed-size per NIC. Overflows if kernel doesn't drain fast enough — usually a CPU/IRQ issue, not a tuning issue. Default sizes (256–4096 entries) are fine; can be bumped via `ethtool -G <iface> rx 4096`.
2. **Kernel socket buffer** (per-socket, configurable via sysctl). If pypylon's `RetrieveResult` doesn't get called fast enough, packets queue here. Default cap is `net.core.rmem_max` (16 MiB typical, sometimes 208 KB on older systems). At 8 cameras × 520 Mbps, peak 1-second buffering = ~520 MB total; we don't need that, but the cap should be raised.

### Tunables (MS-01)

| Knob | What it does | Recommended value |
| --- | --- | --- |
| `net.core.rmem_max` | Max socket receive buffer | **32 MiB** (`33554432`) |
| `net.core.rmem_default` | Default socket receive buffer | **16 MiB** (`16777216`) |
| `net.core.netdev_max_backlog` | Backlog when kernel can't keep up | **5000** (default 1000) |
| `net.core.netdev_budget` | Work per softirq invocation | **600** (default 300) |
| `ethtool -G <iface> rx 4096` | NIC RX ring size | **4096** (max for most Intel NICs) |
| `ip link set <iface> mtu 9000` | Jumbo frames on camera-side NIC | **9000** (must match switch + camera) |
| IRQ affinity | Pin NIC interrupts to specific cores | **Yes, dedicated cores** away from encoder cores |

These go in `/etc/sysctl.d/99-frameforge.conf` and `/etc/network/interfaces.d/` (or netplan). For MS-01 specifically, the 10G SFP+ NIC is typically Intel X550 or X710 — both well-supported with `ethtool` tuning.

### Jetson reality

Jetson Nano J1010 has 1× Realtek RTL8211F gigabit NIC. MTU 9000 *is* supported by this PHY. Currently we leave defaults; for the bench experiment a future Jetson-side change could be:
```bash
sudo ip link set eth0 mtu 9000
```
Combined with `GevSCPSPacketSize=8192` and a switch that supports jumbo. We didn't get to this on Jetson because of the isolation/dongle blocker.

---

## Layer 3 — Application

### Frame flow in-process

```
pylon.InstantCamera.RetrieveResult()        # pulls one frame, blocks up to retrieve_timeout_ms
    ↓
result.GetArray()                            # numpy view of pylon's internal buffer
    ↓
frame_ring.get_free()                        # blocks if no free slot (back-pressure point)
    ↓
np.copyto(frame_ring.view(slot), array)     # ONE copy from pylon buffer → shared-mem slot
    ↓
data_queue.put((slot_index, hw_ts, mono_ts))  # cross-process handoff (just an int, not bytes)
    ↓ ... encoder process picks up ...
data_queue.get()
    ↓
frame_ring.view(slot)                       # zero-copy numpy view into shared memory
    ↓
cv2.cvtColor(..., COLOR_GRAY2BGR)           # mono → BGR (HW encoder wants 3-channel)
    ↓
backend.write(bgr)                          # GStreamer pipeline (HW encode on Jetson)
    ↓
frame_ring.release(slot)                    # return slot to free-list
```

Only one memory copy per frame (pylon → ring). Cross-process handoff is just a slot index. This is the design that meets the 20 ms loop budget at 1 cam; at 8 cams the same flow runs 8× in parallel processes with no inter-process memory copy.

### Back-pressure budget

The ring is the single back-pressure point. `frame_ring.get_free()` blocks when all slots are in flight. If the encoder is slow, acquisition stalls; if `_SLOT_ACQUIRE_TIMEOUT_S` (1.0s) expires, we drop the frame (`acq.overrun_drops` counter).

**Sizing math (per camera):**
- `ring_slots = 128` slots × 1.3 MB = **~167 MB shared memory per camera ring**
- Time buffer = 128 / 50 fps = **2.56 seconds** of slack
- Queue depth `256` is mostly irrelevant since it holds (int, int, int) tuples, not frames. Its only role is fail-fast if encoder dies for >5s.

**For 8 cameras MS-01:** 8 × 167 MB = **~1.34 GB shared memory** total. Fits in 32-64 GB MS-01 RAM trivially. If we wanted longer buffer (e.g. survive a 10s encoder stall), bump `ring_slots` to 512: 8 × 668 MB = ~5.3 GB. Still fine.

### Encoder feed math

Each encoder process does:
```
1. data_queue.get()           # ~µs
2. cv2.cvtColor (mono→BGR)    # ~2-5 ms (3× the pixel count copied)
3. backend.write(bgr)         # ~5-15 ms HW encode on Jetson; faster on MS-01 QSV
4. frame_ring.release()       # ~µs
```

Budget at 50 fps = 20 ms/frame. Currently observed (Jetson, 1 cam): `encode_ms_max` = 23 ms occasionally, mostly 2–10 ms. **Within budget but tight.** The `cvtColor` step is wasteful — encoder backends that accept gray (libx264 with `-pix_fmt gray`) skip this. Decision item for MS-01 backend choice.

### Code references — application knobs

| Variable | File | Current | Notes |
| --- | --- | --- | --- |
| `width`, `height`, `channels` | [config.py:70-72](../frameforge/config.py#L70) | 1280, 1024, 1 | Per-sensor; locked |
| `ring_slots` | [config.py:73](../frameforge/config.py#L73) | 128 | 2.56s buffer at 50fps; revisit at MS-01 scale |
| `queue_depth` | [config.py:74](../frameforge/config.py#L74) | 256 | Slot-index tuples only; rarely the binding limit |
| `encode.fps` | [config.py:29](../frameforge/config.py#L29) | 50.0 | Sensor and recording target |
| `encode.bitrate_mbps` | [config.py:28](../frameforge/config.py#L28) | 0.16 (bench), 1.0 (default) | 0.16 = ~72 MB/hr matches prod CRF 21 target |
| `encode.gop` | [config.py:30](../frameforge/config.py#L30) | 250 | 5-second GOP; matches prod baseline |
| `encode.backend` | [config.py:27](../frameforge/config.py#L27) | `nvv4l2h265enc` | Jetson; MS-01 will pick from `libx264GrayCRF` / `hevcQsv` |
| `_SLOT_ACQUIRE_TIMEOUT_S` | [acquisition.py:29](../frameforge/acquisition.py#L29) | 1.0 | Drop after 1s of ring-full |

---

## Layer 4 — Upload + storage

### Output rates

- Per-camera encoded bandwidth (CBR): `0.16 Mbps`
- Per-camera per-hour: `0.16 × 3600 / 8 = 72 MB`
- 8 cameras per hour: `~576 MB/hr`
- 8 cameras per 30-hour scratch buffer: `~17 GB`

### Scratch sizing

Scratch lives on local NVMe. Sequence:
1. Encoder writes `.mp4.part` continuously during the hour
2. At hour-end, atomic rename `.part → .mp4`
3. Transfer worker scans every `scan_interval_s = 30s`, sees the `.mp4`, uploads to VAST, deletes local

If VAST is unreachable for 30 hours straight, we'd accumulate ~17 GB. **MS-01 NVMe partition for scratch: 100 GB minimum** (gives ~5 days of uplink-down survival). Cheap insurance.

`low_disk_threshold_mb = 500` is fine; under that we log ERROR and increment a gauge. No auto-delete (acquisition back-pressure stops the bleed).

### NVMe write performance

Sustained write of 576 MB/hr = **160 KB/s**. Modern NVMe drives sustain 1–7 GB/s. This is 30,000× under capacity. Not a constraint.

### SMB to VAST

`smbprotocol` library, userspace SMB3 (the Jetson's old `cifs.ko` kernel mount was the blocker on the prior setup; MS-01's modern kernel could use kernel CIFS but we'll keep userspace for portability).

| Knob | File | Default | Notes |
| --- | --- | --- | --- |
| `transfer.scan_interval_s` | [config.py:51](../frameforge/config.py#L51) | 30 | Polls scratch every 30s; lower = faster upload but more wasted scans |
| `transfer.max_attempts_per_chunk` | [config.py:52](../frameforge/config.py#L52) | 30 | At 30s/attempt = 15 min before logging STUCK |
| `transfer.low_disk_threshold_mb` | [config.py:53](../frameforge/config.py#L53) | 500 | Below this, ERROR log + low_disk gauge |
| `_UPLOAD_BUFFER_BYTES` | [transfer.py:37](../frameforge/transfer.py#L37) | 4 MiB | shutil.copyfileobj chunk size; SMB write batching |

VAST upload throughput is not the binding constraint. Even on the Jetson's 1 GbE link, 576 MB/hr = 0.13 MB/s = 1 Mbps; the link can theoretically push ~110 MB/s.

---

## Platform comparison

| Concern | Jetson (today, 1 cam) | MS-01 (target, 8 cams) |
| --- | --- | --- |
| Camera NIC | 1× 1 GbE built-in Realtek | Switch SFP+ → MS-01 SFP+ via 10G DAC |
| Switch | Office LAN (shared, lossy) | Ubiquiti Enterprise 8 PoE (isolated, 2.5G ports + 2× SFP+) |
| Aggregate ingress | 520 Mbps (1 cam) | 4.16 Gbps (8 cams) |
| Link utilization | ~52% (gigabit) | ~42% (10G) |
| MTU | 1500 (not yet tuned) | **9000** target |
| Jumbo frames on switch | N/A | Required; verify Ubiquiti supports + enable |
| Encoder | HW NVENC h264/h265 via GStreamer | libx264 gray (CPU) OR hevc_qsv (Intel iGPU); A/B benchmark |
| RAM for SHM rings | 167 MB (1 cam × 128 slots) | ~1.34 GB (8 cams × 128 slots) |
| Scratch sizing | ~17 GB / 30 hr (similar math) | **100 GB partition recommended** |
| Office uplink | Shared with camera (today) | Separate: MS-01 2.5G RJ45 → wall |

---

## Bottleneck analysis — where does it bind?

**Jetson, 1 camera, shared office LAN (today):**
- Layer 1 (GigE Vision over shared LAN): packet loss is the binding constraint. ~9% incomplete observed.
- Layer 3 (application): acq loop ≈ 20 ms, encode ≈ 5-15 ms — within budget but tight.
- Layer 4 (upload): trivial.

**Jetson, 1 camera, isolated switch (hypothetical):**
- Layer 1: ~0% loss expected; line rate not a problem at 52% utilization.
- Layer 3: same as above.

**MS-01, 8 cameras, isolated 10G switch (target):**
- Layer 1: 42% link utilization; jumbo frames give ~6× packet-rate headroom. Expected loss = 0.
- Layer 2: kernel buffers + IRQ affinity tuning required; default JustWorks for modest counts, intentional for 8 cams.
- Layer 3: **encoder CPU is the wildcard.** libx264 gray at CRF 21 on 8 streams × 1280×1024 × 50fps. MS-01 typically has 6P+8E cores (i9-12900H or similar). Per-stream encode budget at 50fps = 20 ms. Whether libx264 fits depends on preset (`superfast` vs `medium`). hevc_qsv offloads to Intel iGPU and frees the CPU entirely — but loses CRF semantics. A/B benchmark planned.
- Layer 4: trivial.

---

## Recommendations for MS-01 setup

1. **MTU 9000 on the camera-facing SFP+ link.** Configure on Ubiquiti switch (`Switch Settings → Jumbo Frame` in UniFi UI) AND in `/etc/netplan/` on MS-01 for the SFP+ interface.
2. **GevSCPSPacketSize = 8192** in `prod.yaml`.
3. **GevSCPD = 0** initially; revisit if `acq.incomplete` is nonzero after deployment.
4. **Kernel tuning** in `/etc/sysctl.d/99-frameforge.conf`:
   ```
   net.core.rmem_max = 33554432
   net.core.rmem_default = 16777216
   net.core.netdev_max_backlog = 5000
   net.core.netdev_budget = 600
   ```
5. **NIC ring buffer** via systemd service or `/etc/network/interfaces.d/`:
   ```bash
   ethtool -G <sfp-iface> rx 4096
   ```
6. **IRQ affinity**: pin SFP+ NIC IRQs to a dedicated core (P-core preferred); pin encoder processes to other cores. Surfaces only at full 8-camera load.
7. **Scratch partition**: 100 GB on NVMe, mounted at `/var/lib/frameforge/scratch`.
8. **Office uplink** on one of the 2.5G RJ45 ports — physically separate from the camera path.
9. **Backend choice**: build both (`libx264GrayCRF` and `hevcQsv`); benchmark at full 8-camera load before committing prod.yaml default.

---

## Open testing question — proving network loss locally

How do we prove (on Jetson, before MS-01 arrives) that the dropped frames are network loss and not something in our code? Options:

1. **`tc qdisc` packet drop simulation** — Linux traffic control can inject configurable packet loss on a network interface. Install on Jetson:
   ```bash
   sudo tc qdisc add dev eth0 root netem loss 5%
   ```
   Then run acquisition. If `acq.incomplete` rate matches the configured loss rate, we've proven the chain. Reset with `sudo tc qdisc del dev eth0 root`.
2. **pylon `Statistic_Resend_Request_Count`** — pypylon exposes camera-reported resend counts via `camera.GetStreamGrabberParams().Statistic_Resend_Request_Count`. If this matches our `acq.incomplete` count, the camera sees the same loss we do.
3. **Compare incomplete rate vs `netstat -su` UDP packet receive errors** on the Jetson — if kernel-level UDP drops correlate with frame loss, it's the network stack.
4. **`tcpdump` capture during a session** — count packets actually delivered to userspace vs GVSP frame headers. Heavy on disk.

Recommended sequence: **(1) first, then (2) for confirmation.** The `tc netem` test is the cleanest causal proof — we control the loss rate and check whether our metrics reflect it.

---

## References

- [Basler GigE Vision configuration guide](https://docs.baslerweb.com/network-related-camera-parameters)
- [pypylon API](https://github.com/basler/pypylon)
- [Linux kernel networking tuning](https://www.kernel.org/doc/Documentation/networking/scaling.txt)
- [GStreamer nvv4l2h264enc](https://docs.nvidia.com/jetson/l4t-multimedia/group__l4t__mm__nvv4l2h264enc.html)
- [smbprotocol](https://github.com/jborean93/smbprotocol)
