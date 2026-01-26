{
  description = "SocTalk - LLM-powered SOC agent for security alert triage and investigation";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, nixpkgs-unstable, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        
        pkgs-unstable = import nixpkgs-unstable {
          inherit system;
          config.allowUnfree = true;
        };

        # Import local nix modules
        lib = import ./nix/lib.nix { inherit pkgs; };
        
        # Package definitions
        packages = import ./nix/packages { 
          inherit pkgs pkgs-unstable lib; 
          rev = self.rev or "dev";
        };
        
        # Docker image definitions
        images = import ./nix/images { 
          inherit pkgs packages; 
        };
        
        # Development shell
        devShell = import ./nix/shells { 
          inherit pkgs pkgs-unstable; 
        };

      in {
        # Development shell: nix develop
        devShells.default = devShell;

        # Packages: nix build .#<name>
        packages = packages // images // {
          default = packages.soctalk-api;
        };

        # Apps: nix run .#<name>
        apps = {
          api = {
            type = "app";
            program = "${packages.soctalk-api}/bin/soctalk-api";
          };
          orchestrator = {
            type = "app";
            program = "${packages.soctalk-orchestrator}/bin/soctalk";
          };
        };
      }
    );
}
