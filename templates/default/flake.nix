{
  description = "Minimal Tartarus agent with core tools and a dev shell";

  inputs = {
    nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/*";
    tartarus.url = "github:alyraffauf/tartarus";
  };

  outputs = { nixpkgs, tartarus, self, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forEachSystem = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      agents = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        tartarus.lib.mkAgents { inherit pkgs; } (
          import ./agent.nix {
            inherit pkgs;
            agentModules = tartarus.agentModules;
          }
        )
      );

      devShells = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [ tartarus.packages.${system}.tartarus ];
          };
        }
      );

      packages = forEachSystem (
        system:
        {
          default = self.agents.${system}.default.bundle;
        }
      );
    };
}
