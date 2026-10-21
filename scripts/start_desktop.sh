#!/usr/bin/env bash
set -euo pipefail
# Local-only VNC. Access through an SSH tunnel, NEVER publish 6085 to the internet.
export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
sleep 2
fluxbox >/dev/null 2>&1 &
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5901 -quiet >/dev/null 2>&1 &
exec websockify --web=/usr/share/novnc 127.0.0.1:6085 127.0.0.1:5901
