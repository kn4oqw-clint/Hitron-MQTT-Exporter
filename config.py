"""
Configuration for the Hitron Prometheus exporter.

All values are overridable via environment variables, so the same image runs
unchanged in Docker/LXC -- set them in docker-compose.yml or the systemd unit.
No secrets live here anymore (the MQTT/InfluxDB path was removed).
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# The CODA54 serves its data endpoints over HTTPS with a self-signed cert;
# plain HTTP (port 80) is closed. Keep VERIFY_TLS off for the self-signed cert.
MODEM_URL         = os.environ.get("MODEM_URL", "https://192.168.100.1")
VERIFY_TLS        = _bool("VERIFY_TLS", False)

LISTEN_PORT       = _int("LISTEN_PORT", 9705)

# Never poll the modem faster than this, no matter how often Prometheus scrapes.
# The CODA's web server wedges under bursty polling -- keep this >= 300.
MIN_POLL_INTERVAL = _int("MIN_POLL_INTERVAL", 300)

REQUEST_TIMEOUT   = _int("REQUEST_TIMEOUT", 10)
INTER_REQUEST_GAP = float(os.environ.get("INTER_REQUEST_GAP", "1.0"))
