{ pkgs, lib }:

pkgs.stdenv.mkDerivation rec {
  pname = "mock-endpoint";
  version = "0.1.0";

  src = pkgs.lib.cleanSource ../../attack-simulator;

  nativeBuildInputs = [
    pkgs.makeWrapper
  ];

  buildInputs = [
    # Tools used by attack scripts
    pkgs.curl
    pkgs.wget
    pkgs.openssh
    pkgs.sshpass
    pkgs.nettools
    pkgs.iproute2
    pkgs.procps
    pkgs.jq
    pkgs.nmap
    pkgs.netcat
    pkgs.tcpdump
    pkgs.python3
    pkgs.zip
    pkgs.coreutils
    pkgs.bash
    pkgs.gnugrep
    pkgs.gnused
    pkgs.gawk
  ];

  dontBuild = true;
  dontConfigure = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/opt/scripts
    mkdir -p $out/etc/cron.d
    mkdir -p $out/var/log/attack-simulator

    # Copy scripts
    cp scripts/run-attack.sh $out/opt/scripts/
    cp scripts/entrypoint.sh $out/opt/scripts/
    chmod +x $out/opt/scripts/*.sh

    # Copy crontab
    if [ -f crontab ]; then
      cp crontab $out/etc/cron.d/attack-simulator
    fi

    # Create wrapper for run-attack.sh with all tools in PATH
    makeWrapper $out/opt/scripts/run-attack.sh $out/bin/run-attack \
      --prefix PATH : ${pkgs.lib.makeBinPath buildInputs}

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "Mock endpoint for SocTalk testing - triggers MITRE ATT&CK techniques";
    homepage = "https://github.com/soctalk/soctalk";
    license = licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "run-attack";
  };
}
