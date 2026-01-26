{ pkgs, soctalk-frontend }:

let
  # Nginx configuration for serving SvelteKit static files
  nginxConf = pkgs.writeText "nginx.conf" ''
    worker_processes auto;
    error_log /dev/stderr warn;
    pid /tmp/nginx.pid;

    events {
      worker_connections 1024;
    }

    http {
      include ${pkgs.nginx}/conf/mime.types;
      default_type application/octet-stream;

      log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

      access_log /dev/stdout main;

      sendfile on;
      tcp_nopush on;
      tcp_nodelay on;
      keepalive_timeout 65;
      types_hash_max_size 2048;

      # Gzip compression
      gzip on;
      gzip_vary on;
      gzip_proxied any;
      gzip_comp_level 6;
      gzip_types text/plain text/css text/xml application/json application/javascript application/rss+xml application/atom+xml image/svg+xml;

      server {
        listen 5173;
        server_name localhost;
        root /var/www/soctalk;

        # SvelteKit SPA routing - try files, then fall back to index.html
        location / {
          try_files $uri $uri/ /index.html;
        }

        # Cache static assets
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
          expires 1y;
          add_header Cache-Control "public, immutable";
        }

        # Health check endpoint
        location /health {
          access_log off;
          return 200 "healthy\n";
          add_header Content-Type text/plain;
        }
      }
    }
  '';

in pkgs.dockerTools.buildLayeredImage {
  name = "soctalk-frontend";
  tag = "latest";

  contents = [
    pkgs.nginx
    pkgs.cacert
    pkgs.bashInteractive
    pkgs.coreutils
  ];

  # Extra commands to set up the image
  extraCommands = ''
    mkdir -p var/www/soctalk
    mkdir -p tmp
    mkdir -p var/log/nginx
    mkdir -p var/cache/nginx
    
    # Copy built frontend assets
    cp -r ${soctalk-frontend}/share/soctalk-frontend/* var/www/soctalk/ || true
  '';

  config = {
    Cmd = [ "${pkgs.nginx}/bin/nginx" "-c" "${nginxConf}" "-g" "daemon off;" ];
    
    ExposedPorts = {
      "5173/tcp" = {};
    };
    
    Env = [
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
    
    WorkingDir = "/var/www/soctalk";
    
    Labels = {
      "org.opencontainers.image.title" = "SocTalk Frontend";
      "org.opencontainers.image.description" = "SvelteKit dashboard for SocTalk SOC agent";
      "org.opencontainers.image.source" = "https://github.com/soctalk/soctalk";
    };
  };

  maxLayers = 100;
}
