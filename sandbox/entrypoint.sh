#!/bin/bash
set -e
export DISPLAY=:99
RES="${SCREEN_RES:-1280x800x24}"
export SCREEN_RES="$RES"

Xvfb :99 -screen 0 "$RES" -nolisten tcp &
sleep 1
openbox &

mkdir -p ~/.vnc
x11vnc -storepasswd "${VNC_PASSWORD:?set VNC_PASSWORD}" ~/.vnc/passwd
x11vnc -display :99 -rfbauth ~/.vnc/passwd -forever -shared -listen 0.0.0.0 -rfbport 5900 -quiet &

# The executor refuses to start without EXECUTOR_TOKEN (24+ characters).
python3 /opt/executor/executor.py &

# Chrome as a normal desktop app: no remote-debugging port, throwaway profile.
exec google-chrome --user-data-dir=/tmp/chrome-profile --no-first-run --no-default-browser-check \
     --window-position=0,0 --window-size=1280,800 --disable-features=Translate
