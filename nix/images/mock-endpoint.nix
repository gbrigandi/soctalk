{ pkgs, mock-endpoint }:

pkgs.dockerTools.buildLayeredImage {
  name = "soctalk-mock-endpoint";
  tag = "latest";

  contents = [
    mock-endpoint
    pkgs.cacert
    pkgs.tzdata
    pkgs.bashInteractive
    pkgs.coreutils
    
    # Attack simulation tools
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
    pkgs.gnugrep
    pkgs.gnused
    pkgs.gawk
    pkgs.rsyslog
  ];

  # Set up directories
  extraCommands = ''
    mkdir -p var/log/attack-simulator
    mkdir -p tmp/attack-artifacts
    mkdir -p etc
  '';

  config = {
    Entrypoint = [ "${mock-endpoint}/opt/scripts/entrypoint.sh" ];
    
    Env = [
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "WAZUH_MANAGER="
      "WAZUH_AGENT_NAME=mock-endpoint"
      "ATTACK_DELAY=10"
      "ATTACK_INTERVAL=300"
      "PATH=${pkgs.lib.makeBinPath [
        mock-endpoint
        pkgs.coreutils
        pkgs.bashInteractive
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
        pkgs.gnugrep
        pkgs.gnused
        pkgs.gawk
      ]}"
    ];
    
    WorkingDir = "/opt/scripts";
    
    Labels = {
      "org.opencontainers.image.title" = "SocTalk Mock Endpoint";
      "org.opencontainers.image.description" = "Attack simulator for SocTalk testing - triggers MITRE ATT&CK techniques";
      "org.opencontainers.image.source" = "https://github.com/soctalk/soctalk";
    };
  };

  maxLayers = 100;
}
