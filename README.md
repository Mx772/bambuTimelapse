# BambuTimelapse

Automatic timelapse capture for **Bambu Lab 3D printers**. Connects to your printer over local MQTT, takes a screenshot from your RTSP camera on every layer change, and stitches everything into a video when the print finishes — all from a clean dark-themed web UI.

> Built with [Claude Code](https://claude.ai/claude-code) by Anthropic.

---

## Features

- **Automatic capture** — subscribes to the Bambu MQTT broker and fires an RTSP screenshot on every layer (or every N layers)
- **30-second final shot** — after the print reports `FINISH`, waits 30 s then takes one last frame and uses it as the cover image
- **Auto-generate timelapse** — runs `ffmpeg` on print completion; can also be triggered manually from the gallery
- **Live dashboard** — real-time layer progress, temperatures, ETA (wall-clock time), and a live camera preview
- **Gallery** — browse all past prints with cover thumbnails, rename them, watch/download timelapses, delete what you don't need
- **File-name aware** — reads the `subtask_name` from MQTT so directories are named `20240315_143022_Benchy_0_2mm_PLA`
- **Browser tab ETA** — tab title shows `Benchy · 45% · done ~3:45 PM`
- **Timezone support** — set your IANA timezone once; all times render correctly in the UI
- **Resource limits** — Docker CPU/memory caps keep timelapse generation from destabilising the host
- **Multi-arch Docker image** — `linux/amd64` + `linux/arm64` (Raspberry Pi 4/5 compatible)

---

## Quick Start (Docker — recommended)

### 1. Pull the pre-built image

```bash
docker pull ghcr.io/mx772/bambutimelapse:latest
```

### 2. Create a data directory and compose file

```bash
mkdir -p ~/bambu-data
```

**`docker-compose.yml`**

```yaml
services:
  bambu-timelapse:
    image: ghcr.io/mx772/bambutimelapse:latest
    container_name: bambu-timelapse
    restart: unless-stopped
    network_mode: host          # lets the container reach your printer's LAN IP
    volumes:
      - ~/bambu-data:/data
    environment:
      - DATA_DIR=/data
      - TZ=America/New_York     # your IANA timezone
      - FFMPEG_THREADS=2        # ffmpeg encoder thread cap
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 1536M
        reservations:
          cpus: '0.25'
          memory: 256M
```

> **Bridge networking:** if you prefer not to use `network_mode: host`, remove that line and add `ports: ["8088:8088"]`. Make sure the printer IP is reachable from the container.

### 3. Start it

```bash
docker compose up -d
```

Open **http://\<your-host-ip\>:8088** in your browser.

---

## Configuration

Everything is configured through the **Settings** screen in the UI — no config files to edit by hand.

### Printer (Settings → Printer tab)

| Field | Description |
|---|---|
| **IP Address** | Local IP of your Bambu printer (e.g. `192.168.1.50`) |
| **Serial Number** | Found on the printer label or in the Bambu app (e.g. `01S00C123456789`) |
| **LAN Access Code** | Shown under Settings → Network → LAN Only Mode on the printer |

Click **Connect**. The dot in the header turns green when the MQTT connection is live.

> **Newer firmware:** if connection is refused, enable **Developer Mode** on the printer (same menu as LAN Only Mode). This disables Bambu's authorization layer for local API access.

### Camera (Settings → Camera tab)

| Field | Description |
|---|---|
| **RTSP URL** | Full URL including credentials, e.g. `rtsp://user:pass@192.168.1.51:554/stream1` |

Use **Test Capture** to verify the URL and preview a frame before your next print.

### Timelapse (Settings → Timelapse tab)

| Field | Default | Description |
|---|---|---|
| **FPS** | `24` | Output video frame rate |
| **Quality** | `high` | `low` / `medium` / `high` — maps to x264 CRF 28 / 23 / 18 |
| **Capture every N layers** | `1` | Set to `2` or `5` to reduce frame count on very tall prints |
| **Auto-generate on finish** | on | Uncheck to generate manually from the Gallery |
| **Timezone** | `America/New_York` | IANA timezone ID for all displayed times and the tab ETA |

---

## Data Layout

All data lives in the mounted volume:

```
/data/
  config.json          ← saved settings
  prints/
    20240315_143022_Benchy_0_2mm_PLA/
      meta.json         ← layer count, timestamps, state
      cover.jpg         ← final frame (used as thumbnail)
      frames/
        000001.jpg
        000002.jpg
        ...
        final.jpg       ← 30-second post-finish capture
      timelapse.mp4     ← generated video (if exists)
```

Deleting the `timelapse.mp4` file (via the Gallery or the Delete Video button) keeps all frames intact so you can regenerate with different settings later.

---

## Timezone Reference

Common IANA timezone IDs:

| Location | ID |
|---|---|
| US Eastern | `America/New_York` |
| US Central | `America/Chicago` |
| US Mountain | `America/Denver` |
| US Pacific | `America/Los_Angeles` |
| UK / Ireland | `Europe/London` |
| Central Europe | `Europe/Berlin` |
| Japan | `Asia/Tokyo` |
| Australia East | `Australia/Sydney` |

Full list: [Wikipedia — List of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)

---

## Resource Limits

The compose file ships with conservative defaults. Adjust to taste:

```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'     # raise on a beefy server, lower on a Pi
      memory: 1536M   # raise if you print with very high resolution / many layers
```

A second layer of throttling lives inside the app — the `FFMPEG_THREADS` env var caps how many threads the encoder uses (default `2`). This is independent of the Docker CPU limit and keeps encoding from spiking even if the container limit is generous.

---

## Building Locally

```bash
git clone https://github.com/Mx772/bambuTimelapse
cd bambuTimelapse
docker compose up --build
```

### Local dev (without Docker)

Requires Python 3.11+ and `ffmpeg` on your `PATH`.

```bash
pip install -r requirements.txt
DATA_DIR=./data TZ=America/New_York uvicorn app.main:app --host 0.0.0.0 --port 8088 --reload
```

---

## How It Works

```
Bambu Printer (MQTT :8883 / TLS)
        │  layer_num changes
        ▼
  mqtt_client.py  ──► event_queue ──► process_events()
                                            │
                            layer_change    │    print_finish
                                ▼           │         ▼
                        capture_frame()     │   wait 30s → capture_frame()
                        (ffmpeg RTSP)       │   copy → cover.jpg
                                            │
                                            ▼  (if auto_generate)
                                    generate_timelapse()
                                    (ffmpeg glob → mp4)
                                            │
                                     broadcast via WebSocket
                                            │
                                    Browser (Alpine.js UI)
```

---


## License

MIT

---

*This project was written by [Claude Code](https://claude.ai/claude-code), Anthropic's AI coding assistant.*
