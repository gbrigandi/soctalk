{ pkgs, lib, rev }:

let
  python = pkgs.python313;
  
  # Create a Python environment with the package and its dependencies
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
    
    # Web framework
    fastapi
    uvicorn
    sse-starlette
    
    # LangChain/LangGraph ecosystem
    langgraph
    langchain
    langchain-core
    langchain-anthropic
    langchain-openai
    langgraph-checkpoint-postgres
    mcp
    
    # Additional deps that may be needed
    httpx
    anyio
    starlette
  ]);

in pkgs.stdenv.mkDerivation rec {
  pname = "soctalk-api";
  version = "0.1.0";

  src = pkgs.lib.cleanSource ../..;

  nativeBuildInputs = [
    python
    pkgs.makeWrapper
  ];

  buildInputs = [
    pythonEnv
    pkgs.postgresql.lib
  ];

  # Skip standard build phases - we use pip
  dontBuild = true;
  dontConfigure = true;

  installPhase = ''
    runHook preInstall

    # Create directory structure
    mkdir -p $out/lib/python${python.pythonVersion}/site-packages
    mkdir -p $out/bin
    mkdir -p $out/share/soctalk

    # Copy the soctalk package
    cp -r src/soctalk $out/lib/python${python.pythonVersion}/site-packages/

    # Copy alembic migrations
    cp -r alembic $out/share/soctalk/
    cp alembic.ini $out/share/soctalk/

    # Create wrapper script for the API
    makeWrapper ${pythonEnv}/bin/uvicorn $out/bin/soctalk-api \
      --set PYTHONPATH "$out/lib/python${python.pythonVersion}/site-packages:${pythonEnv}/${python.sitePackages}" \
      --add-flags "soctalk.api.app:app" \
      --add-flags "--host 0.0.0.0" \
      --add-flags "--port 8000"

    # Create alembic wrapper
    makeWrapper ${pythonEnv}/bin/alembic $out/bin/soctalk-migrate \
      --set PYTHONPATH "$out/lib/python${python.pythonVersion}/site-packages:${pythonEnv}/${python.sitePackages}" \
      --chdir "$out/share/soctalk"

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "SocTalk API - FastAPI backend for the SOC agent";
    homepage = "https://github.com/soctalk/soctalk";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "soctalk-api";
  };
}
