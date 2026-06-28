{
  description = "Minimal Tartarus agent with core tools and a dev shell";

  inputs = {
    nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/*";
    tartarus.url = "github:alyraffauf/tartarus";
  };

  outputs =
    {
      nixpkgs,
      tartarus,
      self,
      ...
    }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forEachSystem = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      agents = forEachSystem (system: {
        default = tartarus.lib.tartarusAgent {
          inherit system;
          modules = [ ./agent.nix ];
          specialArgs = {
            inherit tartarus;
          };
        };
      });

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

      packages = forEachSystem (system: {
        default = self.agents.${system}.default.config.build.bundle;
      });
    };
}
