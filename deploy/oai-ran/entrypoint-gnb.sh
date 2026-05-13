#!/bin/bash
set -uo pipefail

PREFIX=/opt/oai-gnb
SCRIPTS=/opt/oai-gnb/scripts
echo "=================================="
echo "/proc/sys/kernel/core_pattern=$(cat /proc/sys/kernel/core_pattern)"

# Prefer YAML config (mounted by docker-compose) over legacy .conf
if [ -f "$PREFIX/etc/gnb.yaml" ]; then
  CONFIGFILE=$PREFIX/etc/gnb.yaml
elif [ -f "$PREFIX/etc/gnb.conf" ]; then
  CONFIGFILE=$PREFIX/etc/gnb.conf
else
  echo "No configuration file found. Please mount at $PREFIX/etc/gnb.yaml or $PREFIX/etc/gnb.conf"
  exit 255
fi

echo "=================================="
echo "== Configuration file:"
cat $CONFIGFILE

# enable printing of stack traces on assert
export OAI_GDBSTACKS=1

# --- Start metrics forwarders in background ---
if [ -f "$SCRIPTS/dl_metrics_forwarder.py" ]; then
  echo "=================================="
  echo "== Starting DL metrics forwarder"
  python3 "$SCRIPTS/dl_metrics_forwarder.py" -e live &
  DL_FORWARDER_PID=$!
  echo "DL Forwarder PID: $DL_FORWARDER_PID"
fi

if [ -f "$SCRIPTS/ul_metrics_forwarder.py" ]; then
  echo "=================================="
  echo "== Starting UL metrics forwarder"
  python3 "$SCRIPTS/ul_metrics_forwarder.py" -e live &
  UL_FORWARDER_PID=$!
  echo "UL Forwarder PID: $UL_FORWARDER_PID"
fi

# --- Start UL control API in background ---
if [ -f "$SCRIPTS/UL_control_termination.py" ]; then
  echo "=================================="
  echo "== Starting UL control API (will retry until gNB telnet is ready)"
  (
    cd "$SCRIPTS"
    while true; do
      python3 UL_control_termination.py
      echo "[control-api] Exited, restarting in 2s..."
      sleep 2
    done
  ) &
  CONTROL_PID=$!
  echo "Control API PID: $CONTROL_PID"
fi

# --- Build nr-softmodem command ---
new_args=("$@")
new_args+=("-O" "$CONFIGFILE")

echo "=================================="
echo "== Starting gNB soft modem"
if [[ -v USE_ADDITIONAL_OPTIONS ]]; then
    echo "Additional option(s): ${USE_ADDITIONAL_OPTIONS}"
    for word in ${USE_ADDITIONAL_OPTIONS}; do
        new_args+=("$word")
    done
fi

echo "${new_args[@]}"
exec "${new_args[@]}"
