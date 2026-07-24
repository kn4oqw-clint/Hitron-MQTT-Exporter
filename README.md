# Hitron Cable Modem Prometheus Exporter 📡➡️📊

A small Prometheus exporter for the **Hitron CODA / CODA54** cable modem. It
scrapes the modem's `/data/*.asp` JSON endpoints and exposes signal levels, SNR,
FEC error counters, DOCSIS 3.1 OFDM/OFDMA channels, and the DOCSIS bring-up
state machine as Prometheus metrics for Grafana.

> Converted from the original MQTT/InfluxDB one-shot scraper. The MQTT and
> Home Assistant discovery path has been removed in favour of a single
> long-running Prometheus endpoint.

---

## ⚠️ Read this first: don't wedge your modem

The CODA's embedded web server is fragile. Bursty or high-frequency polling
locks up its management stack until you **physically power-cycle the modem**
(the data plane keeps routing, but `192.168.100.1` stops answering on every
port). This exporter is built to avoid that:

- The modem is polled **at most once per `MIN_POLL_INTERVAL`** (default **300s**),
  no matter how often Prometheus scrapes `/metrics`. Scrapes in between are
  served from cache (`hitron_last_poll_timestamp_seconds` stays flat).
- Endpoints are fetched **sequentially with a 1s gap**, never in parallel.
- Every request has a timeout; a failed poll serves the last good cache and
  reports `hitron_up 0` — it does **not** hammer the modem with retries.

Run it as **one long-lived service**. Do **not** put it behind a cron loop or a
`restart: on-exit` container — that back-to-back polling is exactly what wedges
the modem.

---

## 📊 Metrics

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `hitron_up` | gauge | — | 1 if the last poll fully succeeded |
| `hitron_scrape_duration_seconds` | gauge | — | wall time of the last modem poll |
| `hitron_last_poll_timestamp_seconds` | gauge | — | unix time of last *modem* poll (not scrape) |
| `hitron_info` | gauge | vendor, model, hw/sw version, serial, rf_mac | always 1 |
| `hitron_system_uptime_seconds` | gauge | — | drop to ~0 = modem rebooted/wedged |
| `hitron_link_up` | gauge | — | ethernet link up |
| `hitron_link_speed_mbps` | gauge | — | negotiated speed (e.g. 2500) |
| `hitron_docsis_stage_ok` | gauge | stage | 1 per DOCSIS bring-up stage (ranging, dhcp, registration…) — **best early-warning signal** |
| `hitron_docsis_bpi_operational` | gauge | — | BPI+ TEK operational |
| `hitron_ds_signal_strength_dbmv` | gauge | channel_id, frequency_hz | downstream QAM RX power |
| `hitron_ds_snr_db` | gauge | channel_id, frequency_hz | downstream QAM SNR/MER |
| `hitron_ds_correcteds_total` | counter | channel_id, frequency_hz | corrected FEC codewords |
| `hitron_ds_uncorrectables_total` | counter | channel_id, frequency_hz | **uncorrectable** FEC codewords |
| `hitron_ds_octets_total` | counter | channel_id, frequency_hz | downstream octets |
| `hitron_us_signal_strength_dbmv` | gauge | channel_id, frequency_hz | upstream QAM TX power |
| `hitron_us_bandwidth_hz` | gauge | channel_id, frequency_hz | upstream QAM bandwidth |
| `hitron_ds_ofdm_snr_db` | gauge | receive, frequency_hz | DOCSIS 3.1 downstream OFDM SNR |
| `hitron_ds_ofdm_plc_power_dbmv` | gauge | receive, frequency_hz | OFDM PLC power |
| `hitron_ds_ofdm_locked` | gauge | receive, frequency_hz | PLC+NCP+MDC1 all locked |
| `hitron_ds_ofdm_correcteds_total` / `_uncorrectables_total` | counter | receive, frequency_hz | OFDM FEC counters |
| `hitron_us_ofdma_power_dbmv` | gauge | channel_index, frequency_hz | DOCSIS 3.1 upstream OFDMA TX power |
| `hitron_us_ofdma_bandwidth_mhz` | gauge | channel_index, frequency_hz | OFDMA bandwidth |
| `hitron_us_ofdma_operational` | gauge | channel_index, frequency_hz | OFDMA channel state == OPERATE |

Counter metrics (`*_total`) are exposed via the Prometheus client's counter
family, so use `rate()` / `increase()` on them in Grafana.

---

## ⚙️ Configuration

All settings are environment variables (see `config.py` for defaults):

| Env var | Default | Meaning |
|---|---|---|
| `MODEM_URL` | `https://192.168.100.1` | modem base URL (CODA54 is HTTPS-only) |
| `VERIFY_TLS` | `false` | verify the modem's self-signed cert |
| `LISTEN_PORT` | `9705` | exporter `/metrics` port |
| `MIN_POLL_INTERVAL` | `300` | min seconds between modem polls — keep ≥ 300 |
| `REQUEST_TIMEOUT` | `10` | per-request timeout (s) |
| `INTER_REQUEST_GAP` | `1.0` | delay between sequential endpoint fetches (s) |

---

## 🐳 Run with Docker Compose

```bash
docker compose up -d --build
curl -s localhost:9705/metrics | head
```

## 🐍 Run directly

```bash
pip install -r requirements.txt
python main.py
```

## 🧩 Run as a systemd service (LXC)

```ini
# /etc/systemd/system/hitron-exporter.service
[Unit]
Description=Hitron CODA Prometheus exporter
After=network-online.target

[Service]
Environment=MODEM_URL=https://192.168.100.1
Environment=MIN_POLL_INTERVAL=300
ExecStart=/usr/bin/python3 /opt/hitron-exporter/main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 🔭 Prometheus + alerts

Scrape config and starter alert rules are in [`prometheus/`](prometheus/).
Scrape at whatever interval you like — 60s is fine; the exporter still only
touches the modem every `MIN_POLL_INTERVAL`.

```yaml
scrape_configs:
  - job_name: hitron
    scrape_interval: 60s
    static_configs:
      - targets: ["HITRON_EXPORTER_HOST:9705"]
```

---

## 🧾 License

MIT

## 🧠 Credits

Built by Clint @ [TheChance.Family](https://home.thechance.family) — because modems should be visible too.
