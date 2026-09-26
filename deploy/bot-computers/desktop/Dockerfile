# A bot's computer (PLAN.md A10.f.02): a light Linux desktop the bot drives over the gateway,
# and the user can watch through noVNC. Runs as the non-root user "bot" (uid 1000).
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb fluxbox x11vnc novnc websockify xdotool imagemagick xterm dbus-x11 x11-utils x11-xserver-utils \
        chromium fonts-dejavu fonts-noto-color-emoji \
        python3 python3-pip python3-venv nodejs npm git curl wget ca-certificates procps nano unzip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash bot \
    && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# bots just run `chromium <url>`: through the logging proxy, no sandbox warning (gVisor is the sandbox)
RUN printf '%s\n' 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox --test-type --no-first-run --disable-gpu --password-store=basic ${HTTPS_PROXY:+--proxy-server=$HTTPS_PROXY}"' \
      > /etc/chromium.d/omnibots
COPY start.sh /usr/local/bin/start-desktop
RUN chmod 755 /usr/local/bin/start-desktop

USER bot
WORKDIR /home/bot
EXPOSE 6080
CMD ["/usr/local/bin/start-desktop"]
