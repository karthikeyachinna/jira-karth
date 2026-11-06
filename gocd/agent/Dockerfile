# GoCD agent with what the jira-release-notes jobs need: python3 + venv, Chrome (PDF), git.
FROM gocd/gocd-agent-ubuntu-24.04:v26.1.0
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl git \
      fonts-liberation \
 && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*
USER go
