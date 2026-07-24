#!/usr/bin/env bash
#
# Stand up a Debian LXC on Proxmox running the Hitron Prometheus exporter.
# RUN THIS ON THE PVE HOST (172.16.50.10) as root.
#
# It creates an unprivileged container, installs the exporter natively under
# systemd (no Docker-in-LXC), and verifies it can reach the modem + serve
# /metrics. Re-runnable-ish: it will refuse if $VMID already exists.
#
set -euo pipefail

# ---- tunables ---------------------------------------------------------------
VMID="${VMID:-}"                       # blank = auto-pick next free id
HOSTNAME_="hitron-exporter"
STORAGE="${STORAGE:-local-lvm}"        # container rootfs storage
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"  # where templates live
BRIDGE="${BRIDGE:-vmbr0}"
NET_CIDR="${NET_CIDR:-dhcp}"           # e.g. 10.10.0.50/24  (dhcp = auto)
GATEWAY="${GATEWAY:-}"                 # required only if NET_CIDR is static
VLAN="${VLAN:-}"                       # optional VLAN tag
MEMORY_MB="${MEMORY_MB:-256}"
DISK_GB="${DISK_GB:-2}"
CORES="${CORES:-1}"
MODEM_URL="${MODEM_URL:-https://192.168.100.1}"
MIN_POLL_INTERVAL="${MIN_POLL_INTERVAL:-300}"
REPO="https://github.com/kn4oqw-clint/Hitron-MQTT-Exporter.git"
BRANCH="${BRANCH:-feature/prometheus-exporter}"   # switch to main once PR #1 is merged
# -----------------------------------------------------------------------------

log() { echo -e "\033[1;36m[+]\033[0m $*"; }
die() { echo -e "\033[1;31m[!]\033[0m $*" >&2; exit 1; }

command -v pct >/dev/null || die "pct not found -- run this on the PVE host."

# pick a VMID if not given
if [[ -z "$VMID" ]]; then VMID=$(pvesh get /cluster/nextid); fi
pct status "$VMID" &>/dev/null && die "CT $VMID already exists -- set VMID=<free id>."
log "Using VMID $VMID"

# ensure a debian-12 template is present
TMPL=$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)
[[ -n "$TMPL" ]] || die "No debian-12 template available from pveam."
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TMPL"; then
  log "Downloading template $TMPL"
  pveam download "$TEMPLATE_STORAGE" "$TMPL"
fi
TMPL_REF="$TEMPLATE_STORAGE:vztmpl/$TMPL"

# build net string
NETSTR="name=eth0,bridge=${BRIDGE}"
[[ -n "$VLAN" ]] && NETSTR="${NETSTR},tag=${VLAN}"
if [[ "$NET_CIDR" == "dhcp" ]]; then
  NETSTR="${NETSTR},ip=dhcp"
else
  [[ -n "$GATEWAY" ]] || die "Static NET_CIDR requires GATEWAY=<ip>."
  NETSTR="${NETSTR},ip=${NET_CIDR},gw=${GATEWAY}"
fi

log "Creating CT $VMID ($HOSTNAME_)"
pct create "$VMID" "$TMPL_REF" \
  --hostname "$HOSTNAME_" \
  --cores "$CORES" --memory "$MEMORY_MB" --swap 128 \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --net0 "$NETSTR" \
  --features nesting=1 \
  --unprivileged 1 \
  --onboot 1 \
  --description "Hitron CODA54 Prometheus exporter (:9705)"

log "Starting CT"
pct start "$VMID"
sleep 5

log "Waiting for network in container..."
for i in $(seq 1 30); do
  pct exec "$VMID" -- getent hosts github.com &>/dev/null && break
  sleep 2
  [[ $i -eq 30 ]] && die "Container has no network/DNS -- check bridge/VLAN."
done

log "Installing packages"
pct exec "$VMID" -- bash -lc '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip ca-certificates >/dev/null
'

log "Deploying exporter from $REPO ($BRANCH)"
pct exec "$VMID" -- bash -lc "
  set -e
  rm -rf /opt/hitron-exporter
  git clone --depth 1 -b '$BRANCH' '$REPO' /opt/hitron-exporter
  python3 -m venv /opt/hitron-exporter/.venv
  /opt/hitron-exporter/.venv/bin/pip install -q -r /opt/hitron-exporter/requirements.txt
"

log "Installing systemd unit"
pct exec "$VMID" -- bash -lc "cat >/etc/systemd/system/hitron-exporter.service <<UNIT
[Unit]
Description=Hitron CODA Prometheus exporter
After=network-online.target
Wants=network-online.target

[Service]
Environment=MODEM_URL=${MODEM_URL}
Environment=MIN_POLL_INTERVAL=${MIN_POLL_INTERVAL}
Environment=LISTEN_PORT=9705
ExecStart=/opt/hitron-exporter/.venv/bin/python /opt/hitron-exporter/main.py
Restart=on-failure
RestartSec=30
DynamicUser=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now hitron-exporter.service
"

# ---- verification -----------------------------------------------------------
CT_IP=$(pct exec "$VMID" -- bash -lc "hostname -I | awk '{print \$1}'")
log "Container IP: ${CT_IP:-unknown}"

log "Checking modem reachability from inside the container..."
if pct exec "$VMID" -- bash -lc "curl -sk --max-time 8 -o /dev/null ${MODEM_URL}/data/getSysInfo.asp"; then
  echo "    modem reachable ✔"
else
  echo -e "\033[1;33m    WARNING: container cannot reach ${MODEM_URL}. The modem is on the"
  echo -e "    192.168.100.0/24 side; make sure this container's network can route to it"
  echo -e "    (same path your workstation uses). Exporter will report hitron_up 0 until fixed.\033[0m"
fi

sleep 12   # let the first poll complete
log "Sample metrics:"
pct exec "$VMID" -- bash -lc "curl -s --max-time 8 localhost:9705/metrics | grep -E '^hitron_(up|link_up|link_speed|system_uptime|info)' || echo '    (no metrics yet -- check: journalctl -u hitron-exporter)'"

echo
log "Done. Prometheus scrape target:  ${CT_IP:-<container-ip>}:9705"
log "Logs:   pct exec $VMID -- journalctl -u hitron-exporter -f"
