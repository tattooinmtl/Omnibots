#!/bin/bash
# Start the bot's desktop: a virtual screen, a window manager, a terminal, VNC (with this
# computer's own password, set by the gateway at start) and noVNC on port 6080.
set -u
export DISPLAY=:1 HOME=/home/bot
mkdir -p "$HOME/.fluxbox" "$HOME/Desktop" /tmp/.X11-unix 2>/dev/null

Xvfb :1 -screen 0 1280x800x24 -nolisten tcp &
for _ in $(seq 1 50); do xdpyinfo -display :1 >/dev/null 2>&1 && break; sleep 0.1; done

# fluxbox with no wallpaper tool (fbsetbg can't set one here and pops an error): a navy root instead
printf 'background: none\n' > "$HOME/.fluxbox/overlay"
grep -q styleOverlay "$HOME/.fluxbox/init" 2>/dev/null || printf 'session.styleOverlay: ~/.fluxbox/overlay\nsession.screen0.rootCommand: xsetroot -solid "#0b1226"\n' >> "$HOME/.fluxbox/init"
xsetroot -solid '#0b1226'
fluxbox &
xterm -geometry 110x30+40+40 -bg '#070b18' -fg '#c9d4ee' -title "bot@$(hostname)" &

mkdir -p "$HOME/.vnc"
x11vnc -storepasswd "${VNC_PASSWORD:-changeme}" "$HOME/.vnc/passwd" >/dev/null 2>&1
unset VNC_PASSWORD
x11vnc -display :1 -rfbauth "$HOME/.vnc/passwd" -forever -shared -localhost -rfbport 5900 -quiet -noxdamage &

exec websockify --web /usr/share/novnc 6080 localhost:5900
