{ pkgs }:

let
  python = pkgs.python311;
  
  # Python packages available from nixpkgs
  pythonWithPackages = python.withPackages (ps: with ps; [
    # Build tools
    pip
    setuptools
    wheel
    hatchling
    
    # Dev tools (available in nixpkgs)
    pytest
    pytest-asyncio
    pytest-cov
    mypy
    
    # Some runtime deps that are in nixpkgs
    pydantic
    python-dotenv
    aiohttp
    rich
    structlog
    fastapi
    uvicorn
    sqlalchemy
    alembic
    psycopg2
    greenlet
    
    # Type stubs
    types-requests
  ]);

in pkgs.mkShell {
  name = "soctalk-dev";

  buildInputs = [
    # Python with base packages
    pythonWithPackages

    # Node.js and pnpm for frontend
    pkgs.nodejs_20
    pkgs.nodePackages.pnpm

    # PostgreSQL client tools
    pkgs.postgresql_16

    # Testing
    pkgs.playwright-driver.browsers

    # Linting and formatting
    pkgs.ruff
    pkgs.nodePackages.prettier

    # Build tools
    pkgs.gnumake
    pkgs.gcc
    pkgs.pkg-config

    # Runtime dependencies for Python packages
    pkgs.openssl
    pkgs.openssl.dev
    pkgs.postgresql.lib
    pkgs.libffi

    # Utilities
    pkgs.curl
    pkgs.jq
    pkgs.git

    # For building MCP servers locally (optional)
    pkgs.rustc
    pkgs.cargo
  ];

  shellHook = ''
    echo "SocTalk Development Shell"
    echo "========================="
    echo ""
    
    # Create virtual environment if it doesn't exist
    if [ ! -d .venv ]; then
      echo "Creating Python virtual environment..."
      python -m venv .venv
    fi
    
    # Activate virtual environment
    source .venv/bin/activate
    
    # Install Python dependencies if needed
    if [ ! -f .venv/.installed ]; then
      echo "Installing Python dependencies..."
      # Temporarily set LD_LIBRARY_PATH for pip install (native deps may need it)
      LD_LIBRARY_PATH="$NIX_LD_LIBRARY_PATH" pip install -e ".[dev,slack]" --quiet
      touch .venv/.installed
    fi
    
    echo "Python: $(python --version)"
    echo "Node.js: $(node --version)"
    echo "pnpm: $(pnpm --version)"
    echo "PostgreSQL client: $(psql --version | head -1)"
    echo ""
    echo "Commands:"
    echo "  Backend:"
    echo "    pytest -m 'not integration'  # Run unit tests"
    echo "    pytest -m integration        # Run integration tests"
    echo "    ruff check src/              # Lint Python code"
    echo "    alembic upgrade head         # Run migrations"
    echo "    uvicorn soctalk.api.app:app --reload  # Start API server"
    echo ""
    echo "  Frontend:"
    echo "    cd frontend && pnpm install  # Install deps"
    echo "    cd frontend && pnpm dev      # Start dev server"
    echo "    cd frontend && pnpm check    # Type check"
    echo "    cd frontend && pnpm test     # Run Playwright tests"
    echo ""
    echo "  Nix:"
    echo "    nix build .#soctalk-api      # Build API package"
    echo "    nix build .#soctalk-frontend # Build frontend"
    echo "    nix build .#docker-api       # Build API Docker image"
    echo ""

    # Set up Playwright browsers path
    export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"

    # Ensure PYTHONPATH includes src
    export PYTHONPATH="$PWD/src:$PYTHONPATH"

    # PostgreSQL connection defaults (for local dev)
    export DATABASE_URL="''${DATABASE_URL:-postgresql+asyncpg://soctalk:soctalk@localhost:5432/soctalk}"
    
    # OpenSSL for building packages that need it
    export OPENSSL_DIR="${pkgs.openssl.dev}"
    export OPENSSL_LIB_DIR="${pkgs.openssl.out}/lib"
    export OPENSSL_INCLUDE_DIR="${pkgs.openssl.dev}/include"
  '';

  # Environment variables
  RUST_LOG = "info";
  SOCTALK_LOG_LEVEL = "DEBUG";
  
  # Note: We intentionally do NOT set LD_LIBRARY_PATH globally as it breaks
  # system tools like `nix`. If you need it for pip install, set it locally:
  #   LD_LIBRARY_PATH=$NIX_LD_LIBRARY_PATH pip install ...
  
  # For packages that need native libs during pip install
  NIX_LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
    pkgs.stdenv.cc.cc.lib
    pkgs.openssl
    pkgs.postgresql.lib
    pkgs.zlib
  ];
}
