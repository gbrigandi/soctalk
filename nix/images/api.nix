{ pkgs, soctalk-api }:

pkgs.dockerTools.buildLayeredImage {
  name = "soctalk-api";
  tag = "latest";

  contents = [
    soctalk-api
    pkgs.cacert        # SSL certificates
    pkgs.tzdata        # Timezone data
    pkgs.bashInteractive  # For debugging
    pkgs.coreutils     # Basic utilities
  ];

  config = {
    Cmd = [ "${soctalk-api}/bin/soctalk-api" ];
    
    ExposedPorts = {
      "8000/tcp" = {};
    };
    
    Env = [
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "PYTHONUNBUFFERED=1"
    ];
    
    WorkingDir = "/app";
    
    Labels = {
      "org.opencontainers.image.title" = "SocTalk API";
      "org.opencontainers.image.description" = "FastAPI backend for SocTalk SOC agent";
      "org.opencontainers.image.source" = "https://github.com/soctalk/soctalk";
    };
  };

  # Create smaller layers for better caching
  maxLayers = 100;
}
