{ pkgs }:

# Build frontend as a fixed-output derivation
# This allows network access during build for pnpm install
# The hash must be updated when source or dependencies change
pkgs.stdenv.mkDerivation {
  pname = "soctalk-frontend";
  version = "0.1.0";

  src = pkgs.lib.cleanSource ../../frontend;

  nativeBuildInputs = [
    pkgs.nodejs_20
    pkgs.pnpm
    pkgs.cacert
  ];

  # Fixed-output derivation - allows network during build
  outputHashMode = "recursive";
  outputHashAlgo = "sha256";
  # Update this hash when dependencies or source change:
  # nix build .#soctalk-frontend 2>&1 | grep "got:"
  outputHash = "sha256-FbITXd0Z9orW+Vqfm5r1kQultl/AYKa9xHSQg6RJwOw=";

  buildPhase = ''
    runHook preBuild

    export HOME=$TMPDIR
    export PNPM_HOME=$TMPDIR/.pnpm
    mkdir -p $PNPM_HOME

    # Install dependencies
    pnpm install --frozen-lockfile

    # Build SvelteKit app
    pnpm build

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p $out

    # Static adapter outputs to build/ directory
    if [ -d "build" ]; then
      cp -r build/* $out/
    fi

    # Verify index.html exists (required for static adapter)
    if [ ! -f "$out/index.html" ]; then
      echo "ERROR: index.html not found in build output!"
      echo "Build directory contents:"
      ls -la build/ || true
      exit 1
    fi

    echo "Build output contents:"
    ls -la $out/

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "SocTalk Frontend - SvelteKit dashboard for the SOC agent";
    homepage = "https://github.com/soctalk/soctalk";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
