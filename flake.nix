{
  description = "Tartarus: a Nix-defined containment runtime for auditable agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # uv2nix turns uv.lock into a Nix Python package set, so the harness's Python
    # deps are resolved from the lockfile rather than hand-maintained.
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    # uv does not lock build systems; this overlay supplies backends (hatchling).
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
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

      # The packaged harness (uv2nix virtualenv). `nix build .#tartarus` /
      # `nix run .#tartarus -- "prompt"`. See package.nix.
      packages = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
          tartarus = pkgs.callPackage ./package.nix {
            inherit pyproject-nix uv2nix pyproject-build-systems;
          };
        in
        {
          default = tartarus;
          inherit tartarus;
        }
      );

      # The developer shell for hacking on this harness (Python + pytest). This
      # is distinct from an agent's own `shell`, whose PATH is baked into its
      # bundle. ruff and ty are supplied by `uv`, not this shell.
      devShells = eachSystem (
        system:
        let pkgs = pkgsFor system; in
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
              (python3.withPackages (p: [
                p.httpx
                p.pip
                p.pydantic
                p.pytest
              ]))
            ];
          };
        }
      );
    };
}
