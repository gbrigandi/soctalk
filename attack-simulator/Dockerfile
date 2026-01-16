FROM ubuntu:22.04

LABEL maintainer="soctalk"
LABEL description="Mock Wazuh Endpoint for SocTalk testing - triggers MITRE ATT&CK techniques on bootstrap"

ENV DEBIAN_FRONTEND=noninteractive
ENV WAZUH_MANAGER=""
ENV WAZUH_AGENT_NAME="mock-endpoint"

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    apt-transport-https \
    ca-certificates \
    lsb-release \
    cron \
    sudo \
    openssh-client \
    sshpass \
    net-tools \
    iproute2 \
    procps \
    vim \
    jq \
    nmap \
    netcat-openbsd \
    tcpdump \
    python3 \
    python3-pip \
    zip \
    rsyslog \
    && rm -rf /var/lib/apt/lists/*

# Install Wazuh agent 4.9.2 (matching manager version)
RUN curl -s https://packages.wazuh.com/key/GPG-KEY-WAZUH | gpg --dearmor -o /usr/share/keyrings/wazuh.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" > /etc/apt/sources.list.d/wazuh.list \
    && apt-get update \
    && apt-get install -y wazuh-agent=4.9.2-1 \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /opt/scripts /var/log/attack-simulator /tmp/attack-artifacts

# Copy scripts
COPY scripts/run-attack.sh /opt/scripts/
COPY scripts/entrypoint.sh /opt/scripts/
COPY crontab /etc/cron.d/attack-simulator

# Set permissions
RUN chmod +x /opt/scripts/*.sh \
    && chmod 0644 /etc/cron.d/attack-simulator \
    && crontab /etc/cron.d/attack-simulator

WORKDIR /opt/scripts

ENTRYPOINT ["/opt/scripts/entrypoint.sh"]
