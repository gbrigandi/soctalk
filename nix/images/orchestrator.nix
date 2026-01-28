{ pkgs, soctalk-orchestrator }:

pkgs.dockerTools.buildLayeredImage {
  name = "soctalk-orchestrator";
  tag = "latest";

  contents = [
    soctalk-orchestrator
    pkgs.cacert        # SSL certificates
    pkgs.tzdata        # Timezone data
    pkgs.bashInteractive  # For debugging
    pkgs.coreutils     # Basic utilities
    pkgs.curl          # For health checks
    pkgs.jq            # For JSON processing
    pkgs.iana-etc      # /etc/protocols, /etc/services
    pkgs.glibc         # NSS libraries for DNS resolution (libnss_dns.so, libnss_files.so)
    
    # NSS configuration for DNS resolution in glibc-based binaries (Rust MCP servers)
    (pkgs.writeTextDir "etc/nsswitch.conf" ''
      hosts: files dns
    '')
  ];

  config = {
    Cmd = [ "${soctalk-orchestrator}/bin/soctalk" ];
    
    Env = [
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "PYTHONUNBUFFERED=1"
      # MCP server paths are set by the package wrapper, but can be overridden
      "WAZUH_MCP_SERVER_PATH=${soctalk-orchestrator}/opt/mcp-servers/mcp-server-wazuh"
      "CORTEX_MCP_SERVER_PATH=${soctalk-orchestrator}/opt/mcp-servers/mcp-server-cortex"
      "THEHIVE_MCP_SERVER_PATH=${soctalk-orchestrator}/opt/mcp-servers/mcp-server-thehive"
      "MISP_MCP_SERVER_PATH=${soctalk-orchestrator}/opt/mcp-servers/mcp-server-misp"
    ];
    
    WorkingDir = "/app";
    
    Labels = {
      "org.opencontainers.image.title" = "SocTalk Orchestrator";
      "org.opencontainers.image.description" = "LangGraph workflow orchestrator for SocTalk SOC agent";
      "org.opencontainers.image.source" = "https://github.com/soctalk/soctalk";
    };
  };

  maxLayers = 100;
}
