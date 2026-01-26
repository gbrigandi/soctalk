{ pkgs, packages }:

{
  docker-api = import ./api.nix { 
    inherit pkgs; 
    soctalk-api = packages.soctalk-api;
  };
  
  docker-frontend = import ./frontend.nix { 
    inherit pkgs; 
    soctalk-frontend = packages.soctalk-frontend;
  };
  
  docker-orchestrator = import ./orchestrator.nix { 
    inherit pkgs; 
    soctalk-orchestrator = packages.soctalk-orchestrator;
  };
  
  docker-mock-endpoint = import ./mock-endpoint.nix { 
    inherit pkgs; 
    mock-endpoint = packages.mock-endpoint;
  };
}
