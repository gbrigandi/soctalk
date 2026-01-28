# SocTalk Justfile
# Build and manage Docker images built with Nix

# Registry prefix for tagging images
registry := "cr.lab.atricore.io"

# Default target - show available commands
default:
    @just --list

# Build, load, and tag the API image
build-api:
    @echo "Building API image..."
    nix build .#docker-api
    @echo "Loading image into Docker..."
    docker load < $(nix build .#docker-api --print-out-paths)
    @echo "Tagging image for registry..."
    docker tag soctalk-api:latest {{registry}}/soctalk-api:latest
    @echo "API image ready: {{registry}}/soctalk-api:latest"

# Build, load, and tag the orchestrator image
build-orchestrator:
    @echo "Building orchestrator image..."
    nix build .#docker-orchestrator
    @echo "Loading image into Docker..."
    docker load < $(nix build .#docker-orchestrator --print-out-paths)
    @echo "Tagging image for registry..."
    docker tag soctalk-orchestrator:latest {{registry}}/soctalk-orchestrator:latest
    @echo "Orchestrator image ready: {{registry}}/soctalk-orchestrator:latest"

# Build, load, and tag the frontend image
build-frontend:
    @echo "Building frontend image..."
    nix build .#docker-frontend
    @echo "Loading image into Docker..."
    docker load < $(nix build .#docker-frontend --print-out-paths)
    @echo "Tagging image for registry..."
    docker tag soctalk-frontend:latest {{registry}}/soctalk-frontend:latest
    @echo "Frontend image ready: {{registry}}/soctalk-frontend:latest"

# Build, load, and tag the mock-endpoint image
build-mock-endpoint:
    @echo "Building mock-endpoint image..."
    nix build .#docker-mock-endpoint
    @echo "Loading image into Docker..."
    docker load < $(nix build .#docker-mock-endpoint --print-out-paths)
    @echo "Tagging image for registry..."
    docker tag soctalk-mock-endpoint:latest {{registry}}/soctalk-mock-endpoint:latest
    @echo "Mock-endpoint image ready: {{registry}}/soctalk-mock-endpoint:latest"

# Build all images
build-all: build-api build-orchestrator build-frontend build-mock-endpoint
    @echo ""
    @echo "All images built and tagged:"
    @echo "  - {{registry}}/soctalk-api:latest"
    @echo "  - {{registry}}/soctalk-orchestrator:latest"
    @echo "  - {{registry}}/soctalk-frontend:latest"
    @echo "  - {{registry}}/soctalk-mock-endpoint:latest"

# Run all services using docker-compose
run:
    docker compose -f docker-compose-nix.yml up

# Run all services in detached mode
run-detached:
    docker compose -f docker-compose-nix.yml up -d

# Stop all services
stop:
    docker compose -f docker-compose-nix.yml down

# Show logs for all services
logs:
    docker compose -f docker-compose-nix.yml logs -f

# Push all images to registry
push-all:
    @echo "Pushing images to {{registry}}..."
    docker push {{registry}}/soctalk-api:latest
    docker push {{registry}}/soctalk-orchestrator:latest
    docker push {{registry}}/soctalk-frontend:latest
    docker push {{registry}}/soctalk-mock-endpoint:latest
    @echo "All images pushed to {{registry}}"

# Build and push all images
release: build-all push-all
    @echo "Release complete!"
