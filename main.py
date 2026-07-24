#!/usr/bin/env python3
"""
Prometheus exporter for the Hitron CODA / CODA54 cable modem.

Scrapes the modem's /data/*.asp JSON endpoints and exposes them as Prometheus
metrics on :$LISTEN_PORT/metrics.

WHY THIS IS BUILT THE WAY IT IS
-------------------------------
The CODA's embedded web server is fragile: aggressive or bursty polling wedges
its management stack until a physical power-cycle. This exporter is deliberately
gentle:

  * The modem is polled at most once per MIN_POLL_INTERVAL (default 300s),
    regardless of how often Prometheus scrapes /metrics. Results are cached and
    served to every scrape in between.
  * Endpoints are fetched SEQUENTIALLY with a small gap -- never in parallel,
    never a burst.
  * Every request has a timeout, and a failed poll does not retry aggressively;
    it just serves the last good cache and reports hitron_up 0.

This replaces the previous one-shot MQTT/InfluxDB scraper. Run it as a
long-lived service (systemd or `docker compose up -d`) -- do NOT wrap it in a
cron loop or a restart-on-exit container, which is what hammers the modem.
"""
import json
import re
import time
import threading

import requests
import urllib3
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# endpoint file -> cache key
ENDPOINTS = [
    "getSysInfo.asp",
    "system_model.asp",
    "getLinkStatus.asp",
    "getCMInit.asp",
    "dsinfo.asp",
    "usinfo.asp",
    "dsofdminfo.asp",   # NOTE: correct spelling. The old MQTT script used
                        # "dsofmodinfo.asp" (404), so downstream OFDM -- the
                        # primary DOCSIS 3.1 channel -- was silently missing.
    "usofdminfo.asp",
]

# getCMInit stage -> the string value that means "healthy"
CMINIT_OK = {
    "hwInit": "Success",
    "findDownstream": "Success",
    "ranging": "Success",
    "dhcp": "Success",
    "timeOfday": "Success",
    "downloadCfg": "Success",
    "registration": "Success",
    "networkAccess": "Permitted",
    "trafficStatus": "Enable",
}


def _f(v):
    """Parse a possibly space-padded numeric string; None if not numeric."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _uptime_seconds(s):
    """'00h:05m:42s' or '1d:02h:03m:04s' -> seconds."""
    total, matched = 0, False
    for val, unit in re.findall(r"(\d+)\s*([dhms])", str(s)):
        matched = True
        total += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else None


class HitronCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self._last_poll = 0.0
        self._last_ok = False
        self._last_duration = 0.0
        self._last_poll_wall = 0.0

    # --- modem I/O ----------------------------------------------------------
    def _login(self, session):
        """Best-effort 'nologin' auth. The /data endpoints answer without it,
        but this mirrors the modem UI and keeps session-scoped pages happy."""
        try:
            r = session.post(
                f"{config.MODEM_URL}/goform/home",
                headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"},
                data={"user": "nologin", "pws": "nologin"},
                verify=config.VERIFY_TLS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if r.cookies.get("userid"):
                session.cookies.set("userid", r.cookies.get("userid"))
        except Exception:
            pass  # data endpoints work anyway

    def _fetch(self):
        session = requests.Session()
        session.headers.update({"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"})
        self._login(session)

        data, ok = {}, True
        start = time.monotonic()
        for i, ep in enumerate(ENDPOINTS):
            try:
                r = session.get(f"{config.MODEM_URL}/data/{ep}",
                                verify=config.VERIFY_TLS, timeout=config.REQUEST_TIMEOUT)
                r.raise_for_status()
                data[ep] = json.loads(r.text)
            except Exception:
                ok = False
                if ep in self._cache:      # keep last good value for this endpoint
                    data[ep] = self._cache[ep]
            if i < len(ENDPOINTS) - 1:
                time.sleep(config.INTER_REQUEST_GAP)
        return data, ok, time.monotonic() - start

    def _maybe_poll(self):
        with self._lock:
            due = time.monotonic() - self._last_poll >= config.MIN_POLL_INTERVAL
            if due or not self._cache:
                data, ok, dur = self._fetch()
                self._cache, self._last_ok, self._last_duration = data, ok, dur
                self._last_poll = time.monotonic()
                self._last_poll_wall = time.time()
            return self._cache, self._last_ok, self._last_duration, self._last_poll_wall

    # --- prometheus ---------------------------------------------------------
    def collect(self):
        data, ok, dur, wall = self._maybe_poll()

        yield GaugeMetricFamily("hitron_up",
            "1 if the last modem poll fully succeeded", value=1 if ok else 0)
        yield GaugeMetricFamily("hitron_scrape_duration_seconds",
            "Wall time of the last sequential modem poll", value=dur)
        yield GaugeMetricFamily("hitron_last_poll_timestamp_seconds",
            "Unix time of the last actual modem poll (not the Prometheus scrape)", value=wall)

        # ---- info + system ----------------------------------------------------
        sysinfo = (data.get("getSysInfo.asp") or [{}])[0]
        model = data.get("system_model.asp") or {}
        info = GaugeMetricFamily("hitron_info", "Modem identity/build info",
            labels=["vendor", "model", "hw_version", "sw_version", "serial", "rf_mac"])
        info.add_metric([str(model.get("vendorname", "")), str(model.get("modelName", "")),
                         str(sysinfo.get("hwVersion", "")), str(sysinfo.get("swVersion", "")),
                         str(sysinfo.get("serialNumber", "")), str(sysinfo.get("rfMac", ""))], 1)
        yield info

        up = _uptime_seconds(sysinfo.get("systemUptime"))
        if up is not None:
            yield GaugeMetricFamily("hitron_system_uptime_seconds",
                "Modem uptime; a reset to ~0 means it rebooted or was power-cycled", value=up)

        # ---- ethernet link ----------------------------------------------------
        link = (data.get("getLinkStatus.asp") or [{}])[0]
        yield GaugeMetricFamily("hitron_link_up",
            "1 if the LAN/WAN ethernet link reports Up",
            value=1 if str(link.get("LinkStatus", "")).strip().lower() == "up" else 0)
        spd = _f(re.sub(r"[^\d.]", "", str(link.get("LinkSpeed", ""))))
        if spd is not None:
            yield GaugeMetricFamily("hitron_link_speed_mbps", "Negotiated ethernet link speed (Mbps)", value=spd)

        # ---- DOCSIS registration state machine (best early-warning signal) ----
        cminit = (data.get("getCMInit.asp") or [{}])[0]
        stage = GaugeMetricFamily("hitron_docsis_stage_ok",
            "1 if this DOCSIS bring-up stage is healthy, else 0", labels=["stage"])
        for k, good in CMINIT_OK.items():
            if k in cminit:
                stage.add_metric([k], 1 if str(cminit[k]).strip() == good else 0)
        yield stage
        bpi = str(cminit.get("bpiStatus", ""))
        yield GaugeMetricFamily("hitron_docsis_bpi_operational",
            "1 if BPI+ TEK is operational", value=1 if "operational" in bpi.lower() else 0)

        # ---- downstream QAM ---------------------------------------------------
        ds_l = ["channel_id", "frequency_hz"]
        g_p = GaugeMetricFamily("hitron_ds_signal_strength_dbmv", "Downstream QAM RX power (dBmV)", labels=ds_l)
        g_s = GaugeMetricFamily("hitron_ds_snr_db", "Downstream QAM SNR/MER (dB)", labels=ds_l)
        c_c = CounterMetricFamily("hitron_ds_correcteds", "Downstream QAM corrected FEC codewords", labels=ds_l)
        c_u = CounterMetricFamily("hitron_ds_uncorrectables", "Downstream QAM UNcorrectable FEC codewords", labels=ds_l)
        c_o = CounterMetricFamily("hitron_ds_octets", "Downstream QAM octets", labels=ds_l)
        for ch in data.get("dsinfo.asp") or []:
            lv = [str(ch.get("channelId", "")), str(ch.get("frequency", "")).strip()]
            for src, m in (("signalStrength", g_p), ("snr", g_s),
                           ("correcteds", c_c), ("uncorrect", c_u), ("dsoctets", c_o)):
                if _f(ch.get(src)) is not None:
                    m.add_metric(lv, _f(ch[src]))
        yield from (g_p, g_s, c_c, c_u, c_o)

        # ---- upstream QAM -----------------------------------------------------
        us_l = ["channel_id", "frequency_hz"]
        g_up = GaugeMetricFamily("hitron_us_signal_strength_dbmv", "Upstream QAM TX power (dBmV)", labels=us_l)
        g_ub = GaugeMetricFamily("hitron_us_bandwidth_hz", "Upstream QAM channel bandwidth (Hz)", labels=us_l)
        for ch in data.get("usinfo.asp") or []:
            lv = [str(ch.get("channelId", "")), str(ch.get("frequency", "")).strip()]
            if _f(ch.get("signalStrength")) is not None: g_up.add_metric(lv, _f(ch["signalStrength"]))
            if _f(ch.get("bandwidth")) is not None: g_ub.add_metric(lv, _f(ch["bandwidth"]))
        yield from (g_up, g_ub)

        # ---- downstream OFDM (DOCSIS 3.1) -------------------------------------
        of_l = ["receive", "frequency_hz"]
        g_os = GaugeMetricFamily("hitron_ds_ofdm_snr_db", "Downstream OFDM SNR (dB)", labels=of_l)
        g_op = GaugeMetricFamily("hitron_ds_ofdm_plc_power_dbmv", "Downstream OFDM PLC power (dBmV)", labels=of_l)
        g_ol = GaugeMetricFamily("hitron_ds_ofdm_locked", "1 if PLC+NCP+MDC1 all locked", labels=of_l)
        c_ou = CounterMetricFamily("hitron_ds_ofdm_uncorrectables", "Downstream OFDM uncorrectable codewords", labels=of_l)
        c_oc = CounterMetricFamily("hitron_ds_ofdm_correcteds", "Downstream OFDM corrected codewords", labels=of_l)
        for ch in data.get("dsofdminfo.asp") or []:
            lv = [str(ch.get("receive", "")), str(ch.get("Subcarr0freqFreq", "")).strip()]
            if _f(ch.get("SNR")) is not None: g_os.add_metric(lv, _f(ch["SNR"]))
            if _f(ch.get("plcpower")) is not None: g_op.add_metric(lv, _f(ch["plcpower"]))
            locked = all(str(ch.get(k, "")).strip().upper() == "YES" for k in ("plclock", "ncplock", "mdc1lock"))
            g_ol.add_metric(lv, 1 if locked else 0)
            if _f(ch.get("uncorrect")) is not None: c_ou.add_metric(lv, _f(ch["uncorrect"]))
            if _f(ch.get("correcteds")) is not None: c_oc.add_metric(lv, _f(ch["correcteds"]))
        yield from (g_os, g_op, g_ol, c_ou, c_oc)

        # ---- upstream OFDMA (DOCSIS 3.1) --------------------------------------
        uo_l = ["channel_index", "frequency_hz"]
        g_uop = GaugeMetricFamily("hitron_us_ofdma_power_dbmv", "Upstream OFDMA reported TX power (dBmV)", labels=uo_l)
        g_uob = GaugeMetricFamily("hitron_us_ofdma_bandwidth_mhz", "Upstream OFDMA channel bandwidth (MHz)", labels=uo_l)
        g_uoo = GaugeMetricFamily("hitron_us_ofdma_operational", "1 if OFDMA channel state is OPERATE", labels=uo_l)
        for ch in data.get("usofdminfo.asp") or []:
            lv = [str(ch.get("uschindex", "")), str(ch.get("frequency", "")).strip()]
            if _f(ch.get("repPower")) is not None: g_uop.add_metric(lv, _f(ch["repPower"]))
            if _f(ch.get("channelBw")) is not None: g_uob.add_metric(lv, _f(ch["channelBw"]))
            g_uoo.add_metric(lv, 1 if str(ch.get("state", "")).strip().upper() == "OPERATE" else 0)
        yield from (g_uop, g_uob, g_uoo)


if __name__ == "__main__":
    REGISTRY.register(HitronCollector())
    start_http_server(config.LISTEN_PORT)
    print(f"Hitron Prometheus exporter on :{config.LISTEN_PORT}/metrics -- "
          f"modem {config.MODEM_URL} polled at most every {config.MIN_POLL_INTERVAL}s")
    while True:
        time.sleep(3600)
