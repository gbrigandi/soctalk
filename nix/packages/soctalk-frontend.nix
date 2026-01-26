{ pkgs }:

pkgs.stdenv.mkDerivation rec {
  pname = "soctalk-frontend";
  version = "0.1.0";

  src = pkgs.lib.cleanSource ../../frontend;

  nativeBuildInputs = [
    pkgs.nodejs_20
    pkgs.nodePackages.pnpm
    pkgs.cacert  # For HTTPS during pnpm install
  ];

  # Disable default phases that don't apply
  dontConfigure = true;

  # pnpm needs a writable home and store
  buildPhase = ''
    runHook preBuild
    
    export HOME=$TMPDIR
    export PNPM_HOME=$TMPDIR/.pnpm-home
    export PNPM_STORE_DIR=$TMPDIR/.pnpm-store
    mkdir -p $PNPM_HOME $PNPM_STORE_DIR
    
    # Install dependencies
    pnpm install --frozen-lockfile
    
    # Build for production
    pnpm build
    
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    
    mkdir -p $out/share/soctalk-frontend
    
    # SvelteKit with adapter-auto typically outputs to build/
    if [ -d "build" ]; then
      cp -r build/* $out/share/soctalk-frontend/
    fi
    
    # For adapter-node, also copy the handler
    if [ -d "build/server" ]; then
      mkdir -p $out/lib/soctalk-frontend
      cp -r build/server/* $out/lib/soctalk-frontend/
      cp package.json $out/lib/soctalk-frontend/
    fi
    
    runHook postInstall
  '';

  # Allow network access during build for pnpm (needed in sandbox)
  # Note: For pure builds, you'd need to pre-fetch deps with pnpm2nix or similar
  __noChroot = true;

  meta = with pkgs.lib; {
    description = "SocTalk Frontend - SvelteKit dashboard for the SOC agent";
    homepage = "https://github.com/soctalk/soctalk";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
