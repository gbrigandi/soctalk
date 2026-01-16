#!/bin/bash
#
# SocTalk Development Environment Runner
#
# This script starts all components needed to run SocTalk end-to-end:
# - PostgreSQL database (via Docker)
# - FastAPI backend API
# - SvelteKit frontend
#
# Usage:
#   ./scripts/run-dev.sh [command]
#
# Commands:
#   start    - Start all services (default)
#   stop     - Stop all services
#   status   - Show status of services
#   logs     - Show logs from all services
#   db       - Start only database
#   api      - Start only backend API
#   ui       - Start only frontend
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# PID files
PID_DIR="$PROJECT_ROOT/.pids"
API_PID_FILE="$PID_DIR/api.pid"
FRONTEND_PID_FILE="$PID_DIR/frontend.pid"

# Configuration
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
DB_PORT="${DB_PORT:-5432}"
CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:5173,http://localhost:3000}"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

ensure_pid_dir() {
    mkdir -p "$PID_DIR"
}

check_port() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0  # Port in use
    else
        return 1  # Port free
    fi
}

wait_for_port() {
    local port=$1
    local service=$2
    local timeout=${3:-30}
    local count=0

    while ! check_port $port; do
        sleep 1
        count=$((count + 1))
        if [ $count -ge $timeout ]; then
            log_error "$service failed to start on port $port (timeout after ${timeout}s)"
            return 1
        fi
    done
    return 0
}

start_database() {
    log_info "Starting PostgreSQL database..."

    if check_port $DB_PORT; then
        log_warn "PostgreSQL already running on port $DB_PORT"
        return 0
    fi

    cd "$PROJECT_ROOT"

    if [ -f "docker-compose.yml" ]; then
        docker compose up -d postgres 2>/dev/null || docker-compose up -d postgres

        if wait_for_port $DB_PORT "PostgreSQL" 30; then
            log_success "PostgreSQL started on port $DB_PORT"
        else
            log_error "Failed to start PostgreSQL"
            return 1
        fi
    else
        log_warn "docker-compose.yml not found. Skipping database."
        log_warn "API will run without database persistence."
    fi
}

stop_database() {
    log_info "Stopping PostgreSQL database..."
    cd "$PROJECT_ROOT"

    if [ -f "docker-compose.yml" ]; then
        docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
        log_success "PostgreSQL stopped"
    fi
}

start_api() {
    log_info "Starting FastAPI backend..."

    if check_port $API_PORT; then
        log_warn "Backend API already running on port $API_PORT"
        return 0
    fi

    cd "$PROJECT_ROOT"
    ensure_pid_dir

    # Activate virtual environment and start uvicorn
    export CORS_ORIGINS="$CORS_ORIGINS"

    if [ -f ".venv/bin/activate" ]; then
        (
            source .venv/bin/activate
            nohup uvicorn soctalk.api.app:app \
                --host "$API_HOST" \
                --port "$API_PORT" \
                > "$PROJECT_ROOT/logs/api.log" 2>&1 &
            echo $! > "$API_PID_FILE"
        )

        if wait_for_port $API_PORT "Backend API" 15; then
            log_success "Backend API started on http://localhost:$API_PORT"
        else
            log_error "Failed to start Backend API"
            return 1
        fi
    else
        log_error "Python virtual environment not found. Run: python -m venv .venv && pip install -e ."
        return 1
    fi
}

stop_api() {
    log_info "Stopping FastAPI backend..."

    if [ -f "$API_PID_FILE" ]; then
        local pid=$(cat "$API_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$API_PID_FILE"
    fi

    # Also kill any stray uvicorn processes
    pkill -f "uvicorn soctalk.api" 2>/dev/null || true

    log_success "Backend API stopped"
}

start_frontend() {
    log_info "Starting SvelteKit frontend..."

    if check_port $FRONTEND_PORT; then
        log_warn "Frontend already running on port $FRONTEND_PORT"
        return 0
    fi

    cd "$PROJECT_ROOT/frontend"
    ensure_pid_dir

    if [ -f "package.json" ]; then
        # Install dependencies if node_modules doesn't exist
        if [ ! -d "node_modules" ]; then
            log_info "Installing frontend dependencies..."
            pnpm install
        fi

        nohup pnpm dev --port "$FRONTEND_PORT" > "$PROJECT_ROOT/logs/frontend.log" 2>&1 &
        echo $! > "$FRONTEND_PID_FILE"

        if wait_for_port $FRONTEND_PORT "Frontend" 30; then
            log_success "Frontend started on http://localhost:$FRONTEND_PORT"
        else
            log_error "Failed to start Frontend"
            return 1
        fi
    else
        log_error "Frontend package.json not found"
        return 1
    fi
}

stop_frontend() {
    log_info "Stopping SvelteKit frontend..."

    if [ -f "$FRONTEND_PID_FILE" ]; then
        local pid=$(cat "$FRONTEND_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$FRONTEND_PID_FILE"
    fi

    # Also kill any stray vite processes
    pkill -f "vite.*frontend" 2>/dev/null || true
    pkill -f "node.*soctalk.*frontend" 2>/dev/null || true

    log_success "Frontend stopped"
}

start_all() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║           SocTalk Development Environment                 ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    # Create logs directory
    mkdir -p "$PROJECT_ROOT/logs"

    start_database
    start_api
    start_frontend

    echo ""
    echo "═══════════════════════════════════════════════════════════"
    log_success "All services started!"
    echo ""
    echo "  Frontend:  http://localhost:$FRONTEND_PORT"
    echo "  API:       http://localhost:$API_PORT"
    echo "  API Docs:  http://localhost:$API_PORT/docs"
    echo "  Health:    http://localhost:$API_PORT/health"
    echo ""
    echo "  Logs:"
    echo "    API:      $PROJECT_ROOT/logs/api.log"
    echo "    Frontend: $PROJECT_ROOT/logs/frontend.log"
    echo ""
    echo "  Stop all:  ./scripts/run-dev.sh stop"
    echo "═══════════════════════════════════════════════════════════"
}

stop_all() {
    echo ""
    log_info "Stopping all SocTalk services..."
    echo ""

    stop_frontend
    stop_api
    stop_database

    echo ""
    log_success "All services stopped"
}

show_status() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║              SocTalk Service Status                       ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    # Check PostgreSQL
    if check_port $DB_PORT; then
        echo -e "  PostgreSQL:  ${GREEN}● Running${NC} (port $DB_PORT)"
    else
        echo -e "  PostgreSQL:  ${RED}○ Stopped${NC}"
    fi

    # Check API
    if check_port $API_PORT; then
        echo -e "  Backend API: ${GREEN}● Running${NC} (port $API_PORT)"
        # Try to hit health endpoint
        if curl -s "http://localhost:$API_PORT/health" >/dev/null 2>&1; then
            echo -e "               ${GREEN}  Health: OK${NC}"
        fi
    else
        echo -e "  Backend API: ${RED}○ Stopped${NC}"
    fi

    # Check Frontend
    if check_port $FRONTEND_PORT; then
        echo -e "  Frontend:    ${GREEN}● Running${NC} (port $FRONTEND_PORT)"
    else
        echo -e "  Frontend:    ${RED}○ Stopped${NC}"
    fi

    echo ""
}

show_logs() {
    echo ""
    log_info "Showing logs (Ctrl+C to exit)..."
    echo ""

    if [ -f "$PROJECT_ROOT/logs/api.log" ] || [ -f "$PROJECT_ROOT/logs/frontend.log" ]; then
        tail -f "$PROJECT_ROOT/logs/api.log" "$PROJECT_ROOT/logs/frontend.log" 2>/dev/null
    else
        log_warn "No log files found. Start services first."
    fi
}

# Main entry point
case "${1:-start}" in
    start)
        start_all
        ;;
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 2
        start_all
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    db)
        start_database
        ;;
    api)
        start_api
        ;;
    ui|frontend)
        start_frontend
        ;;
    help|--help|-h)
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  start     Start all services (default)"
        echo "  stop      Stop all services"
        echo "  restart   Restart all services"
        echo "  status    Show status of services"
        echo "  logs      Tail logs from all services"
        echo "  db        Start only database"
        echo "  api       Start only backend API"
        echo "  ui        Start only frontend"
        echo ""
        echo "Environment variables:"
        echo "  API_PORT       Backend API port (default: 8000)"
        echo "  FRONTEND_PORT  Frontend port (default: 5173)"
        echo "  DB_PORT        PostgreSQL port (default: 5432)"
        echo "  CORS_ORIGINS   Allowed CORS origins"
        ;;
    *)
        log_error "Unknown command: $1"
        echo "Run '$0 help' for usage"
        exit 1
        ;;
esac
