#!/bin/bash
#
# Mock Wazuh Endpoint Entrypoint
# Registers with Wazuh and triggers attacks immediately
#

set -e

LOG_FILE="/var/log/attack-simulator/startup.log"
mkdir -p "$(dirname "$LOG_FILE")" /tmp/attack-artifacts

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Mock Wazuh Endpoint Starting ==="

# Configure Wazuh agent
configure_wazuh() {
    log "Configuring Wazuh agent..."

    if [[ -z "$WAZUH_MANAGER" ]]; then
        log "ERROR: WAZUH_MANAGER not set"
        exit 1
    fi

    log "Wazuh Manager: ${WAZUH_MANAGER}"
    log "Agent Name: ${WAZUH_AGENT_NAME:-mock-endpoint}"

    cat > /var/ossec/etc/ossec.conf << EOF
<ossec_config>
  <client>
    <server>
      <address>${WAZUH_MANAGER}</address>
      <port>1514</port>
      <protocol>tcp</protocol>
    </server>
    <notify_time>10</notify_time>
    <time-reconnect>60</time-reconnect>
    <auto_restart>yes</auto_restart>
  </client>

  <syscheck>
    <disabled>no</disabled>
    <frequency>60</frequency>
    <scan_on_start>yes</scan_on_start>
    <directories check_all="yes" realtime="yes" report_changes="yes">/etc,/usr/bin,/usr/sbin,/bin,/sbin</directories>
    <directories check_all="yes" realtime="yes">/tmp/attack-artifacts</directories>
  </syscheck>

  <rootcheck>
    <disabled>no</disabled>
    <frequency>300</frequency>
  </rootcheck>

  <localfile>
    <log_format>syslog</log_format>
    <location>/var/log/syslog</location>
  </localfile>

  <localfile>
    <log_format>syslog</log_format>
    <location>/var/log/auth.log</location>
  </localfile>

  <localfile>
    <log_format>syslog</log_format>
    <location>/var/log/attack-simulator/*.log</location>
  </localfile>

  <wodle name="syscollector">
    <disabled>no</disabled>
    <interval>1h</interval>
    <scan_on_start>yes</scan_on_start>
    <packages>yes</packages>
    <os>yes</os>
    <network>yes</network>
    <ports all="no">yes</ports>
    <processes>yes</processes>
  </wodle>
</ossec_config>
EOF
}

# Register and start agent
start_agent() {
    log "Registering agent with Wazuh manager..."
    /var/ossec/bin/agent-auth -m "$WAZUH_MANAGER" -A "${WAZUH_AGENT_NAME:-mock-endpoint}" 2>&1 || {
        log "Registration note: agent may already exist"
    }

    log "Starting Wazuh agent..."
    /var/ossec/bin/wazuh-control start 2>&1 || true
    sleep 5

    if /var/ossec/bin/wazuh-control status 2>/dev/null | grep -q "running"; then
        log "Wazuh agent is running"
    else
        log "WARNING: Wazuh agent may not be running - check logs"
        cat /var/ossec/logs/ossec.log 2>/dev/null | tail -20 || true
    fi
}

# Run bootstrap attacks immediately
run_bootstrap() {
    log "=== BOOTSTRAP ATTACKS STARTING ==="

    # Give agent time to connect
    sleep 10

    # Run all attacks
    /opt/scripts/run-attack.sh all 2>&1 | tee -a "$LOG_FILE"

    log "=== BOOTSTRAP ATTACKS COMPLETE ==="
}

# Continuous random attacks
attack_loop() {
    local interval="${ATTACK_INTERVAL:-300}"
    log "Starting attack loop (interval: ${interval}s)"

    while true; do
        sleep "$interval"
        log "Running random attack..."
        /opt/scripts/run-attack.sh random 2>&1 | tee -a "$LOG_FILE"
    done
}

# Main
main() {
    configure_wazuh
    start_agent

    # Start rsyslog for logger commands
    service rsyslog start 2>/dev/null || rsyslogd 2>/dev/null || true

    # Start cron
    service cron start 2>/dev/null || cron || true

    # Run bootstrap attacks in background
    run_bootstrap &

    # Start continuous loop in background
    attack_loop &

    log "=== Mock Endpoint Ready ==="
    log "Alerts flowing to Wazuh -> SocTalk"

    # Keep alive
    tail -f /var/log/attack-simulator/*.log 2>/dev/null || sleep infinity
}

main "$@"
