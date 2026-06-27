{
  description = "Tartarus: a Nix-defined containment runtime for auditable agents";

  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0";

  outputs =
    { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      inherit (nixpkgs) lib;
      eachSystem = lib.genAttrs supportedSystems;
      pkgsFor = system: import nixpkgs { inherit system; };

      # The reusable compiler from real Nix capabilities to agent bundles.
      agentsLib = import ./lib/agents.nix { inherit lib; };
    in
    {
      lib = agentsLib;

      # `.#agents.<system>.<name>.bundle` is the shareable runtime boundary the
      # Python harness consumes. We ship one agent named `default`; the lib
      # supports many, so downstream flakes call `agentsLib.mkAgents` with their
      # own named set.
      agents = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
          packages = { }; # optional: add this flake's own derivations when capabilities need them
        in
        agentsLib.mkAgents { inherit pkgs packages; } (import ./agent.nix { inherit pkgs; })
      );

      # The developer shell for hacking on this harness (Python + pytest). This is
      # distinct from an agent's own `shell`, whose PATH is baked into its bundle.
      devShells = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShellNoCC {
            packages = with pkgs; [
              bash
              coreutils
              findutils
              git
              jq
              nixfmt
              ripgrep
              gnused
              (python3.withPackages (pythonPackages: [
                pythonPackages.httpx
                pythonPackages.pip
                pythonPackages.pytest
              ]))
            ];
          };
        }
      );
    };
}
