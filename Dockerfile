FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/soctalk ./src/soctalk
COPY alembic ./alembic
COPY alembic.ini ./

# Install Python dependencies
RUN pip install --no-cache-dir .

# Expose port
EXPOSE 8000

# Run the API server
CMD ["uvicorn", "soctalk.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
