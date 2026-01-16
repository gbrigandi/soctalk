#!/bin/bash
#
# Random Attack Simulator for Wazuh SIEM testing
# Executes random Linux-specific MITRE ATT&CK techniques using Atomic Red Team
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TECHNIQUES_FILE="${SCRIPT_DIR}/linux-techniques.txt"
LOG_FILE="/var/log/attack-simulator/attacks.log"
ATOMICS_PATH="/opt/atomic-red-team/atomics"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Read techniques from file (excluding comments and empty lines)
get_techniques() {
    grep -v '^#' "$TECHNIQUES_FILE" | grep -v '^$' | grep -v '^[[:space:]]*$'
}

# Select a random technique
select_random_technique() {
    local techniques
    techniques=$(get_techniques)
    local count
    count=$(echo "$techniques" | wc -l)
    local random_line
    random_line=$((RANDOM % count + 1))
    echo "$techniques" | sed -n "${random_line}p"
}

# Check if technique has Linux support
check_linux_support() {
    local technique_id="$1"
    local atomics_dir="${ATOMICS_PATH}/${technique_id}"

    if [[ -d "$atomics_dir" ]]; then
        # Check if any test supports linux
        if grep -rq "linux" "${atomics_dir}/" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Execute atomic test
run_atomic_test() {
    local technique_id="$1"
    local test_number="$2"
    local description="$3"

    log "=== Starting Attack Simulation ==="
    log "Technique: ${technique_id}"
    log "Test Number: ${test_number}"
    log "Description: ${description}"

    # Check if technique exists
    if ! check_linux_support "$technique_id"; then
        log "WARNING: Technique ${technique_id} may not have Linux support, attempting anyway..."
    fi

    # Run the atomic test
    log "Executing Atomic Red Team test..."

    local pwsh_cmd
    if [[ "$test_number" == "0" ]]; then
        # Run all tests for this technique
        pwsh_cmd="Import-Module invoke-atomicredteam; Invoke-AtomicTest ${technique_id} -PathToAtomicsFolder '${ATOMICS_PATH}' -Force -Confirm:\$false 2>&1"
    else
        # Run specific test
        pwsh_cmd="Import-Module invoke-atomicredteam; Invoke-AtomicTest ${technique_id} -TestNumbers ${test_number} -PathToAtomicsFolder '${ATOMICS_PATH}' -Force -Confirm:\$false 2>&1"
    fi

    # Execute and capture output
    local output
    local exit_code=0
    output=$(pwsh -Command "$pwsh_cmd" 2>&1) || exit_code=$?

    echo "$output" >> "$LOG_FILE"

    if [[ $exit_code -eq 0 ]]; then
        log "SUCCESS: Attack simulation completed"
    else
        log "NOTE: Test completed with exit code ${exit_code} (may be expected for some tests)"
    fi

    # Attempt cleanup after a delay
    sleep 5
    log "Running cleanup..."
    local cleanup_cmd
    if [[ "$test_number" == "0" ]]; then
        cleanup_cmd="Import-Module invoke-atomicredteam; Invoke-AtomicTest ${technique_id} -PathToAtomicsFolder '${ATOMICS_PATH}' -Cleanup -Force -Confirm:\$false 2>&1"
    else
        cleanup_cmd="Import-Module invoke-atomicredteam; Invoke-AtomicTest ${technique_id} -TestNumbers ${test_number} -PathToAtomicsFolder '${ATOMICS_PATH}' -Cleanup -Force -Confirm:\$false 2>&1"
    fi
    pwsh -Command "$cleanup_cmd" >> "$LOG_FILE" 2>&1 || true

    log "=== Attack Simulation Complete ==="
    echo ""
}

# Main execution
main() {
    log "Attack Simulator starting..."

    # Parse command line arguments
    local mode="${1:-random}"
    local technique_id="${2:-}"
    local test_number="${3:-1}"

    case "$mode" in
        random)
            # Select and run a random technique
            local selected
            selected=$(select_random_technique)

            if [[ -z "$selected" ]]; then
                log "ERROR: No techniques found in ${TECHNIQUES_FILE}"
                exit 1
            fi

            # Parse the selected line
            IFS=':' read -r tech_id test_num desc <<< "$selected"
            run_atomic_test "$tech_id" "$test_num" "$desc"
            ;;

        specific)
            # Run a specific technique
            if [[ -z "$technique_id" ]]; then
                log "ERROR: Technique ID required for specific mode"
                echo "Usage: $0 specific <TECHNIQUE_ID> [TEST_NUMBER]"
                exit 1
            fi
            run_atomic_test "$technique_id" "$test_number" "Manual execution"
            ;;

        list)
            # List available techniques
            echo "Available Linux techniques:"
            get_techniques
            ;;

        all)
            # Run all techniques sequentially
            log "Running all techniques..."
            while IFS=':' read -r tech_id test_num desc; do
                run_atomic_test "$tech_id" "$test_num" "$desc"
                # Random delay between attacks (30-120 seconds)
                local delay=$((RANDOM % 91 + 30))
                log "Waiting ${delay} seconds before next attack..."
                sleep "$delay"
            done < <(get_techniques)
            ;;

        *)
            echo "Usage: $0 [random|specific|list|all] [TECHNIQUE_ID] [TEST_NUMBER]"
            echo ""
            echo "Modes:"
            echo "  random   - Run a random technique (default)"
            echo "  specific - Run a specific technique by ID"
            echo "  list     - List available techniques"
            echo "  all      - Run all techniques with delays"
            exit 1
            ;;
    esac
}

main "$@"
