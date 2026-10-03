FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc xdotool scrot xclip imagemagick \
      python3 curl wget ca-certificates \
      fonts-liberation fonts-noto fonts-noto-color-emoji dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

# Chrome must be the Google .deb (Ubuntu's chromium package is a snap and does not work in containers).
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1500 -s /bin/bash agent
# Root-owned, so the agent user (and Chrome) cannot modify the executor.
COPY executor.py /opt/executor/executor.py
COPY testpage.html /opt/testpage.html
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh && chmod 644 /opt/executor/executor.py /opt/testpage.html
USER agent
WORKDIR /home/agent
ENTRYPOINT ["/entrypoint.sh"]
