{ pkgs, lib, rev }:

let
  python = pkgs.python313;

  # Python environment with orchestrator dependencies
  pythonEnv = python.withPackages (ps: with ps; [
    # Core dependencies (from pyproject.toml)
    pydantic
    python-dotenv
    aiohttp
    rich
    structlog

    # Database
    sqlalchemy
    sqlmodel
    asyncpg
    greenlet
    alembic
    psycopg2

    # Web framework (for API calls)
    fastapi
    uvicorn
    httpx
    anyio
    starlette

    # LangChain/LangGraph ecosystem
    langgraph
    langchain
    langchain-core
    langchain-anthropic
    langchain-openai
    langgraph-checkpoint-postgres
    mcp
    
    # HIL backends
    slack-bolt
  ]);

  # MCP server binaries - fetch from GitHub releases
  # NOTE: These use fakeHash and will fail on first build.
  # Update with the correct hash from the error message.

  mcpServerWazuh = pkgs.stdenv.mkDerivation {
    pname = "mcp-server-wazuh";
    version = "latest";

    src = pkgs.fetchurl {
      url = "https://github.com/gbrigandi/mcp-server-wazuh/releases/latest/download/mcp-server-wazuh-linux-amd64";
      #sha256 = pkgs.lib.fakeHash;
      sha256 = "sha256-ihjLRAB9SDTud66JdTVfQExJvajA1oblNJlONRQTSDQ=";
    };

    dontUnpack = true;

    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/mcp-server-wazuh
      chmod +x $out/bin/mcp-server-wazuh
    '';
  };

  mcpServerCortex = pkgs.stdenv.mkDerivation {
    pname = "mcp-server-cortex";
    version = "latest";

    src = pkgs.fetchurl {
      url = "https://github.com/gbrigandi/mcp-server-cortex/releases/latest/download/mcp-server-cortex-linux-amd64";
      #sha256 = pkgs.lib.fakeHash;
      sha256 = "sha256-qWINP8Hmo8aSTcLsAWXoVTNwgsUHrnrPho3GL1o36N4=";
    };

    dontUnpack = true;

    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/mcp-server-cortex
      chmod +x $out/bin/mcp-server-cortex
    '';
  };

  mcpServerThehive = pkgs.stdenv.mkDerivation {
    pname = "mcp-server-thehive";
    version = "latest";

    src = pkgs.fetchurl {
      url = "https://github.com/gbrigandi/mcp-server-thehive/releases/latest/download/mcp-server-thehive-linux-amd64";
      #sha256 = pkgs.lib.fakeHash;
      sha256 = "sha256-Vxv2vxzV5TOhXB28t2ys1cNAATdV/hMu0ddPrYVKMmI=";
    };

    dontUnpack = true;

    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/mcp-server-thehive
      chmod +x $out/bin/mcp-server-thehive
    '';
  };

  mcpServerMisp = pkgs.stdenv.mkDerivation {
    pname = "mcp-server-misp";
    version = "latest";

    src = pkgs.fetchurl {
      url = "https://github.com/gbrigandi/mcp-server-misp/releases/latest/download/mcp-server-misp-linux-amd64";
      #sha256 = pkgs.lib.fakeHash;
      sha256 = "sha256-3JoqknzBYKYe8xEVLWspvStthQOvFAonUnPMsEPVbHA=";
    };

    dontUnpack = true;

    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/mcp-server-misp
      chmod +x $out/bin/mcp-server-misp
    '';
  };

in pkgs.stdenv.mkDerivation rec {
  pname = "soctalk-orchestrator";
  version = "0.1.0";

  src = pkgs.lib.cleanSource ../..;

  nativeBuildInputs = [
    python
    pkgs.makeWrapper
  ];

  buildInputs = [
    pythonEnv
    pkgs.postgresql.lib
    pkgs.openssl
  ];

  dontBuild = true;
  dontConfigure = true;

  installPhase = ''
    runHook preInstall

    # Create directory structure
    mkdir -p $out/lib/python${python.pythonVersion}/site-packages
    mkdir -p $out/bin
    mkdir -p $out/opt/mcp-servers

    # Copy the soctalk package
    cp -r src/soctalk $out/lib/python${python.pythonVersion}/site-packages/

    # Copy MCP server binaries
    cp ${mcpServerWazuh}/bin/mcp-server-wazuh $out/opt/mcp-servers/
    cp ${mcpServerCortex}/bin/mcp-server-cortex $out/opt/mcp-servers/
    cp ${mcpServerThehive}/bin/mcp-server-thehive $out/opt/mcp-servers/
    cp ${mcpServerMisp}/bin/mcp-server-misp $out/opt/mcp-servers/

    # Create wrapper script for the orchestrator
    makeWrapper ${pythonEnv}/bin/python $out/bin/soctalk \
      --set PYTHONPATH "$out/lib/python${python.pythonVersion}/site-packages:${pythonEnv}/${python.sitePackages}" \
      --set WAZUH_MCP_SERVER_PATH "$out/opt/mcp-servers/mcp-server-wazuh" \
      --set CORTEX_MCP_SERVER_PATH "$out/opt/mcp-servers/mcp-server-cortex" \
      --set THEHIVE_MCP_SERVER_PATH "$out/opt/mcp-servers/mcp-server-thehive" \
      --set MISP_MCP_SERVER_PATH "$out/opt/mcp-servers/mcp-server-misp" \
      --add-flags "-m soctalk.main"

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "SocTalk Orchestrator - LangGraph workflow for security alert triage";
    homepage = "https://github.com/soctalk/soctalk";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "soctalk";
  };
}
