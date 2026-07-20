FROM python:3.13-slim

WORKDIR /app

ARG HELM_VERSION=v3.16.4
ARG HELM_SHA256_AMD64=fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179
ARG HELM_SHA256_ARM64=d3f8f15b3d9ec8c8678fbf3280c3e5902efabe5912e2f9fcf29107efbc8ead69
ARG HELM_CACERT_FILE=/etc/ssl/certs/ca-certificates.crt
# Added this line so Docker explicitly listens for the command-line override
ARG HELM_ALLOW_INSECURE=0

RUN --mount=type=secret,id=helm_ca \
  apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    ca-certificates \
  && arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
     amd64) helm_arch=amd64; helm_sha=$HELM_SHA256_AMD64 ;; \
     arm64) helm_arch=arm64; helm_sha=$HELM_SHA256_ARM64 ;; \
     *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
     esac \
  && if [ -f /run/secrets/helm_ca ]; then CACERT=/run/secrets/helm_ca; else CACERT=$HELM_CACERT_FILE; fi \
  && if [ "${HELM_ALLOW_INSECURE:-1}" = "1" ]; then CURL_OPTS="-k"; else CURL_OPTS="--cacert \"$CACERT\""; fi \
  && eval curl -k --retry 3 --connect-timeout 15 $CURL_OPTS -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz" \
  && echo "${helm_sha}  /tmp/helm.tgz" | sha256sum -c - \
  && tar -xzf /tmp/helm.tgz -C /tmp \
  && mv "/tmp/linux-${helm_arch}/helm" /usr/local/bin/helm \
  && chmod +x /usr/local/bin/helm \
  && rm -rf /tmp/helm.tgz "/tmp/linux-${helm_arch}" /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/soctalk ./src/soctalk
COPY src/soctalk_wire ./src/soctalk_wire
COPY src/soctalk_entities ./src/soctalk_entities
COPY alembic ./alembic
COPY alembic.ini ./
COPY charts/soctalk-tenant ./charts/soctalk-tenant
COPY charts/wazuh ./charts/wazuh

# Fix permissions for the non-root execution process
RUN find /app -type f -exec chmod a+r {} + \
 && find /app -type d -exec chmod a+rx {} +

# Force pip and its build subprocesses (hatchling) to trust PyPI domains over the proxy
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"

# Install Python dependencies
RUN pip install --no-cache-dir .

# Expose port
EXPOSE 8000

# Run the API server
CMD ["uvicorn", "soctalk.core.api.app_v1:app", "--host", "0.0.0.0", "--port", "8000"]