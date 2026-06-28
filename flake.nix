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
      ];
      inherit (nixpkgs) lib;
      eachSystem = lib.genAttrs supportedSystems;
      pkgsFor = system: import nixpkgs { inherit system; };

      # The reusable compiler from real Nix capabilities to agent bundles.
      agentsLib = import ./lib/agents.nix { inherit lib; };
      agentModules = import ./agent-modules { inherit lib; };
    in
    {
      lib = agentsLib;
      inherit agentModules;

      # `.#agents.<system>.<name>.bundle` is the shareable runtime boundary the
      # Python harness consumes. We ship one agent named `default`; the lib
      # supports many, so downstream flakes call `agentsLib.mkAgents` with their
      # own named set.
      agents = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
        in
        agentsLib.mkAgents { inherit pkgs; } (import ./agent.nix { inherit pkgs agentModules; })
      );

      checks = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
          moduleNames = [
            "bash"
            "read"
            "write"
            "edit"
            "glob"
            "list"
            "grep"
            "web_fetch"
          ];
          resolvedCatalog = agentsLib.resolveCapabilities { inherit pkgs; } (
            map (moduleName: agentModules.${moduleName}) moduleNames
          );
          defaultManifest =
            (agentsLib.mkAgents { inherit pkgs; } (import ./agent.nix { inherit pkgs agentModules; }))
            .default.manifest;
          catalogCapabilityNames = lib.attrNames resolvedCatalog;
          expectedCapabilityNames = [
            "bash"
            "edit"
            "glob"
            "grep"
            "list"
            "read"
            "web_fetch"
            "write"
          ];
          globCapability = defaultManifest.capabilities.glob;
          defaultToolNames = map (tool: tool.name) defaultManifest.tools;
          # Keep in sync with the same list in tests/test_bundle.py
          # (test_default_flake_bundle_loads_and_is_self_contained).
          expectedDefaultTools = [
            "background_bash"
            "bash"
            "bg_output"
            "bg_status"
            "bg_stop"
            "edit"
            "fetch_rfc"
            "format_nix"
            "git_diff"
            "git_log"
            "git_show"
            "git_status"
            "glob"
            "grep"
            "jq"
            "list"
            "pypi_versions"
            "pytest"
            "read"
            "web_fetch"
            "write"
            "write_artifact"
          ];
          checksPassed =
            lib.assertMsg (
              (builtins.sort builtins.lessThan catalogCapabilityNames)
              == (builtins.sort builtins.lessThan expectedCapabilityNames)
            ) "agentModules must resolve to the expected capability names"
            && lib.assertMsg (
              (builtins.sort builtins.lessThan defaultToolNames)
              == (builtins.sort builtins.lessThan expectedDefaultTools)
            ) "default agent must expose the curated practical tool set"
            && lib.assertMsg (defaultManifest.capabilities ? glob) "default agent must expose glob"
            && lib.assertMsg (
              globCapability.policy == "auto"
              && globCapability.grants.writable == [ ]
              && globCapability.grants.network.allowedHosts == [ ]
              && globCapability.grants.packageBins != [ ]
            ) "glob must stay read-only and carry a package grant"
            && lib.assertMsg (
              defaultManifest.capabilities.shell_escape.grants.unrestricted
              && !(builtins.elem "shell_escape" defaultToolNames)
            ) "shell_escape must stay denied and absent from tools";
        in
        {
          agent-modules = pkgs.runCommand "tartarus-agent-modules-check" { } ''
            ${lib.optionalString checksPassed "touch $out"}
          '';
        }
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

      templates.default = {
        path = ./templates/default;
        description = "Minimal Tartarus agent with core tools and a dev shell";
        welcomeText = ''
          Your Tartarus agent is ready.

            nix develop

          Set TARTARUS_API_KEY (or OPENCODE_API_KEY) before running the agent.
        '';
      };
    };
}
