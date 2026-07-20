# SocTalk Justfile
# Build and manage Docker images

# Registry prefix for tagging images
registry := "cr.lab.atricore.io"

# Default target - show available commands
default:
    @just --list

# Build and tag the API image
build-api:
    @echo "Building API image..."
    @if [ -n "${HELM_CA_PATH:-}" ]; then \
        echo "Using BuildKit secret from HELM_CA_PATH=${HELM_CA_PATH}"; \
        DOCKER_BUILDKIT=1 docker build --secret id=helm_ca,src="${HELM_CA_PATH}" -f Dockerfile --network=host -t soctalk-api:latest .; \
    else \
        DOCKER_BUILDKIT=1 docker build -f Dockerfile --network=host -t soctalk-api:latest .; \
    fi
    @echo "Tagging image for registry..."
    docker tag soctalk-api:latest {{registry}}/soctalk-api:latest
    @echo "API image ready: {{registry}}/soctalk-api:latest"

# Build and tag the orchestrator image
build-orchestrator:
    @echo "Building orchestrator image..."
    docker build -f Dockerfile.orchestrator --network=host -t soctalk-orchestrator:latest .
    @echo "Tagging image for registry..."
    docker tag soctalk-orchestrator:latest {{registry}}/soctalk-orchestrator:latest
    @echo "Orchestrator image ready: {{registry}}/soctalk-orchestrator:latest"

# Build and tag the per-tenant adapter image (Dockerfile.adapter ->
# soctalk-adapter). The soctalk-tenant chart runs this as the adapter
# sidecar. The controller defaults to
# ghcr.io/soctalk/soctalk-adapter:0.1.13-fixes; dev/values.local.yaml
# repoints it at this local tag for k3d. No CI dependency for local work.
build-adapter:
    @echo "Building adapter image..."
    docker build -f Dockerfile.adapter --network=host -t soctalk-adapter:latest .
    @echo "Tagging image for registry..."
    docker tag soctalk-adapter:latest {{registry}}/soctalk-adapter:latest
    @echo "Adapter image ready: {{registry}}/soctalk-adapter:latest"

# Build and tag the frontend image
build-frontend:
    @echo "Building frontend image..."
    docker build -f Dockerfile.frontend --network=host -t soctalk-frontend:latest .
    @echo "Tagging image for registry..."
    docker tag soctalk-frontend:latest {{registry}}/soctalk-frontend:latest
    @echo "Frontend image ready: {{registry}}/soctalk-frontend:latest"

# Build and tag the canonical V1 app-ui image (Dockerfile.app → soctalk-app-ui)
#
# This is the image the soctalk-system Helm chart references
# (component "app-ui" → soctalk-app-ui), and what CI publishes
# (.github/workflows/publish-images.yml). Distinct from build-frontend,
# which builds the legacy compose-era soctalk-frontend from
# Dockerfile.frontend. The chart / k8s deploy path needs THIS one.
build-app-ui:
    @echo "Building app-ui image..."
    docker build -f Dockerfile.app --network=host -t soctalk-app-ui:latest .
    @echo "Tagging image for registry..."
    docker tag soctalk-app-ui:latest {{registry}}/soctalk-app-ui:latest
    @echo "App-ui image ready: {{registry}}/soctalk-app-ui:latest"

# Build and tag the mock-endpoint image
build-mock-endpoint:
    @echo "Building mock-endpoint image..."
    docker build -f attack-simulator/Dockerfile --network=host -t soctalk-mock-endpoint:latest attack-simulator/
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
    docker compose up

# Run all services in detached mode
run-detached:
    docker compose up -d

# Stop all services
stop:
    docker compose down

# Show logs for all services
logs:
    docker compose logs -f

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

# ---------------------------------------------------------------------------
# Integration testing — local Postgres with the three V1 SocTalk roles.
# ---------------------------------------------------------------------------
#
# The V1 RLS / migration / IR integration tests under tests/v1/ require:
#   - postgres:16-alpine on port 5432
#   - three roles (soctalk_admin CREATEROLE, soctalk_app, soctalk_mssp BYPASSRLS)
#   - alembic migrations applied up to head
#
# CI bootstraps these inline in .github/workflows/v1-ci.yml. These recipes
# stand up the same shape locally so ``pytest -m integration`` sees what CI
# sees.
#
# Each Python-invoking recipe runs through ``direnv exec .`` to load the
# Nix dev shell's environment (Python, psql, libstdc++ via LD_LIBRARY_PATH,
# etc.). The dev shell hook in nix/shells/default.nix is responsible for
# setting LD_LIBRARY_PATH; recipes here do not duplicate that.

# Brings up two containers:
#   - soctalk-postgres on 5432  (V1 multi-tenant suite; 3 roles + migrations)
#   - soctalk-postgres-test on 5433 (legacy single-tenant recovery suite;
#                                    docker-compose.test.yml, ephemeral)

# Start integration Postgres (3 SocTalk roles + head migration; idempotent)
integration-up:
    docker compose up -d postgres
    docker compose -f docker-compose.test.yml up -d
    scripts/wait-for-pg.sh
    CONTAINER=soctalk-postgres-test PGUSER=soctalk_test PGDATABASE=soctalk_test \
        scripts/wait-for-pg.sh
    docker exec -i soctalk-postgres psql -U soctalk -d soctalk -v ON_ERROR_STOP=1 \
        < scripts/pg-bootstrap-roles.sql
    @direnv exec . bash -c '\
        export DATABASE_URL=postgresql+psycopg2://soctalk_admin:soctalk_admin@localhost:5432/soctalk; \
        .venv/bin/alembic upgrade head'
    @echo ""
    @echo "Integration Postgres ready:"
    @echo "  localhost:5432  V1 multi-tenant (3 roles, migrations applied)"
    @echo "    soctalk_admin / soctalk_admin   (DDL, RLS-subject)"
    @echo "    soctalk_app   / soctalk_app     (runtime, RLS-subject)"
    @echo "    soctalk_mssp  / soctalk_mssp    (BYPASSRLS)"
    @echo "  localhost:5433  legacy single-tenant (tmpfs, schema created per test)"
    @echo "    soctalk_test  / soctalk_test    (superuser)"
    @echo ""
    @echo "Run tests with:  just integration-test"

# Stop integration Postgres (preserves data volume)
integration-down:
    docker compose stop postgres
    docker compose -f docker-compose.test.yml stop

# Destroy integration Postgres + data volume (next integration-up re-bootstraps)
integration-wipe:
    docker compose down postgres
    docker compose -f docker-compose.test.yml down
    docker volume rm soctalk_postgres_data 2>/dev/null || true

# Run V1 integration tests against the local Postgres (defaults to tests/v1; pass paths/flags to narrow)
integration-test *EXTRA="tests/v1":
    @direnv exec . bash -c '\
        export DATABASE_URL_ADMIN=postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5432/soctalk; \
        export DATABASE_URL_APP=postgresql+asyncpg://soctalk_app:soctalk_app@localhost:5432/soctalk; \
        export DATABASE_URL_MSSP=postgresql+asyncpg://soctalk_mssp:soctalk_mssp@localhost:5432/soctalk; \
        M=tests/v1/test_metrics_bridge_tenant_scope.py; \
        if [ "{{EXTRA}}" = "tests/v1" ]; then \
            .venv/bin/pytest -m integration tests/v1 --ignore="$M" && \
            .venv/bin/pytest -m integration "$M"; \
        else \
            .venv/bin/pytest -m integration {{EXTRA}}; \
        fi'

# ---------------------------------------------------------------------------
# Layer C — deploy SocTalk into the local k3d cluster.
# ---------------------------------------------------------------------------
#
# Prerequisites:
#   - scripts/local-up.sh has been run (cluster ``soctalk-local`` is up)
#   - dev/values.local.yaml exists (committed; LLM key NOT committed —
#     see the file header for how to inject one)
#
# First-light flow:
#   just system-up
#   # add /etc/hosts entries (see CONTRIBUTING.md "Layer C: deploying
#   # SocTalk on the local k3d cluster")
#   # open http://devlab.soctalk.local:8080 in a browser
#
# Inner dev loop after a code change:
#   just system-reload
#
# Done for the day:
#   just system-down

# Build SocTalk images, import into the soctalk-local cluster, helm install / upgrade
system-up:
    just build-api build-app-ui
    k3d image import \
        cr.lab.atricore.io/soctalk-api:latest \
        cr.lab.atricore.io/soctalk-app-ui:latest \
        --cluster soctalk-local
    just tenant-images
    helm upgrade --install soctalk-system charts/soctalk-system \
        --namespace soctalk-system --create-namespace \
        -f dev/values.local.yaml \
        --wait
    @echo ""
    @echo "soctalk-system installed. Watch:"
    @echo "  kubectl -n soctalk-system get pods -w"
    @echo ""
    @echo "UI:  http://devlab.soctalk.local:8080  (requires /etc/hosts)"

# Rebuild images, re-import, rolling-restart deployments (skips helm)
system-reload:
    just build-api build-app-ui
    k3d image import \
        cr.lab.atricore.io/soctalk-api:latest \
        cr.lab.atricore.io/soctalk-app-ui:latest \
        --cluster soctalk-local
    kubectl -n soctalk-system rollout restart deploy
    kubectl -n soctalk-system rollout status deploy --timeout=3m

# Helm-uninstall the release and delete the namespace
system-down:
    -helm -n soctalk-system uninstall soctalk-system
    -kubectl delete ns soctalk-system --ignore-not-found

# Build + import the per-tenant images (adapter + runs-worker) into the
# soctalk-local cluster. The runs-worker reuses the orchestrator image
# with a flipped entrypoint (python -m soctalk.runs_worker.main, set by
# the chart). pullPolicy is IfNotPresent, so the kubelet uses the
# k3d-imported image with no registry round-trip. dev/values.local.yaml
# points the controller at these tags via
# tenantProvisioning.{adapter,runsWorker}Image{Repo,Tag}.
#
# Run standalone while iterating on the adapter, then re-provision (or
# `kubectl -n tenant-<slug> rollout restart deploy`) to pick up changes
# on existing tenants. `just system-up` calls this automatically.
tenant-images:
    just build-adapter build-orchestrator
    k3d image import \
        cr.lab.atricore.io/soctalk-adapter:latest \
        cr.lab.atricore.io/soctalk-orchestrator:latest \
        --cluster soctalk-local
    @echo "Per-tenant images imported into soctalk-local."
