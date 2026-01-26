{ pkgs, lib, rev ? "dev" }:

{
  soctalk-api = import ./soctalk-api.nix { 
    inherit pkgs lib rev; 
  };
  
  soctalk-frontend = import ./soctalk-frontend.nix { 
    inherit pkgs; 
  };
  
  soctalk-orchestrator = import ./soctalk-orchestrator.nix { 
    inherit pkgs lib rev; 
  };
  
  mock-endpoint = import ./mock-endpoint.nix { 
    inherit pkgs lib; 
  };
}
