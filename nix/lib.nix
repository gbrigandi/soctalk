{ pkgs }:

{
  # Python version to use across all packages
  python = pkgs.python311;

  # Common Python dependencies from pyproject.toml
  # These are propagated to all Python packages
  pythonDeps = ps: with ps; [
    # Core dependencies
    pydantic
    python-dotenv
    aiohttp
    rich
    structlog
    
    # Database
    sqlmodel
    asyncpg
    greenlet
    alembic
    psycopg2
    
    # Web framework
    fastapi
    uvicorn
    sse-starlette
    
    # LangChain ecosystem (from nixpkgs where available)
    # Note: Some packages may need to be installed via pip in the dev shell
  ];

  # Dev dependencies
  pythonDevDeps = ps: with ps; [
    pytest
    pytest-asyncio
    pytest-cov
    ruff
    mypy
  ];

  # MCP server binary fetchers
  # These fetch pre-built binaries from GitHub releases
  fetchMcpServer = { name, owner ? "gbrigandi", repo ? "mcp-server-${name}", version ? "latest" }:
    pkgs.stdenv.mkDerivation {
      pname = "mcp-server-${name}";
      version = version;
      
      src = pkgs.fetchurl {
        url = if version == "latest" 
          then "https://github.com/${owner}/${repo}/releases/latest/download/${repo}-linux-amd64"
          else "https://github.com/${owner}/${repo}/releases/download/${version}/${repo}-linux-amd64";
        # Note: sha256 must be provided per-server, using fakeSha256 for initial build
        sha256 = pkgs.lib.fakeSha256;
      };

      dontUnpack = true;
      
      installPhase = ''
        mkdir -p $out/bin
        cp $src $out/bin/mcp-server-${name}
        chmod +x $out/bin/mcp-server-${name}
      '';

      meta = with pkgs.lib; {
        description = "MCP server for ${name}";
        homepage = "https://github.com/${owner}/${repo}";
        platforms = [ "x86_64-linux" ];
      };
    };

  # Wazuh agent package (extracted from .deb)
  wazuhAgent = pkgs.stdenv.mkDerivation rec {
    pname = "wazuh-agent";
    version = "4.9.2";

    src = pkgs.fetchurl {
      url = "https://packages.wazuh.com/4.x/apt/pool/main/w/wazuh-agent/wazuh-agent_${version}-1_amd64.deb";
      sha256 = pkgs.lib.fakeSha256;  # Will fail on first build, update with correct hash
    };

    nativeBuildInputs = [ pkgs.dpkg ];

    unpackPhase = ''
      dpkg-deb -x $src .
    '';

    installPhase = ''
      mkdir -p $out
      cp -r var/ossec/* $out/ || true
      cp -r etc/ossec-init.conf $out/etc/ || true
    '';

    meta = with pkgs.lib; {
      description = "Wazuh Agent for security monitoring";
      homepage = "https://wazuh.com";
      platforms = [ "x86_64-linux" ];
    };
  };
}
