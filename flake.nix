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
      self,
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
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      agentsLib = import ./lib/agents.nix { inherit lib nixpkgs; };
      modules = import ./agent-modules { inherit lib; };
    in
    {
      lib = agentsLib;
      inherit modules;

      agents = eachSystem (system: {
        default = agentsLib.tartarusAgent {
          inherit system;
          modules = [ ./agent.nix ];
          specialArgs = {
            tartarus = self;
          };
        };
      });

      checks = eachSystem (
        system:
        let
          pkgs = pkgsFor system;
          evalModuleAgent =
            moduleList:
            agentsLib.tartarusAgent {
              inherit system;
              modules = moduleList;
              specialArgs = {
                tartarus = self;
              };
            };
          evalFails = agent: !(builtins.tryEval (builtins.deepSeq agent.config.build.manifest true)).success;
          # A single-capability agent named `bad`, for the failure-case checks.
          badCap = capability: evalModuleAgent [ { capabilities.bad = capability; } ];
          defaultManifest = self.agents.${system}.default.config.build.manifest;
          minimalAgent = evalModuleAgent [
            {
              capabilities.read_package_json = {
                description = "Read package.json from the work tree.";
                policy = "auto";
                runner = "cat package.json";
              };
            }
          ];
          profileAgent = evalModuleAgent [
            modules.coding
          ];
          # An agent declaring a context policy: only the set fields are emitted,
          # so null fields stay absent for env/default resolution in the harness.
          contextAgent = evalModuleAgent [
            {
              context = {
                maxChars = 5000;
                recentTurns = 3;
                autoCompact = true;
              };
              capabilities.read_package_json = {
                description = "Read package.json from the work tree.";
                policy = "auto";
                runner = "cat package.json";
              };
            }
          ];
          inlineAgent = evalModuleAgent [
            {
              capabilities.read_package_json = {
                description = "Read package.json with jq.";
                policy = "auto";
                params = { };
                grants.packages = [ pkgs.jq ];
                runner = "jq . package.json";
              };
            }
          ];
          multipleAgents = {
            default = evalModuleAgent [ modules.read ];
            research = evalModuleAgent [ modules.webFetch ];
          };
          # A bad capability is rejected by one of two fail-closed layers, tested
          # separately so a regression in either surfaces on its own.

          # Layer 1: the module schema — option types and the required `policy`
          # option reject a malformed declaration before any rule runs.
          schemaFailureAgents = {
            missing-policy = badCap { runner = "true"; };
            invalid-policy = badCap {
              policy = "sometimes";
              runner = "true";
            };
            invalid-grants = badCap {
              policy = "auto";
              runner = "true";
              grants = "bad";
            };
            negative-context-max-chars = evalModuleAgent [ { context.maxChars = -1; } ];
          };

          # Layer 2: capabilityAssertions — each case is otherwise type-valid, so
          # it can only fail via the one capability rule it names. Every rule has a
          # case here.
          validationFailureAgents = {
            unrestricted-auto = badCap {
              policy = "auto";
              runner = "true";
              grants.unrestricted = true;
            };
            background-timeout = badCap {
              policy = "ask-always";
              kind = "background";
              timeout = 1;
              runner = "true";
            };
            background-unrestricted = badCap {
              policy = "ask-always";
              kind = "background";
              runner = "true";
              grants.unrestricted = true;
            };
            control-missing-control = badCap {
              policy = "auto";
              kind = "control";
            };
            control-on-command = badCap {
              policy = "auto";
              control = "status";
              runner = "true";
            };
            control-runner = badCap {
              policy = "auto";
              kind = "control";
              control = "status";
              runner = "true";
            };
            control-grants = badCap {
              policy = "auto";
              kind = "control";
              control = "status";
              grants.packages = [ pkgs.bash ];
            };
            command-missing-runner = badCap { policy = "auto"; };
          };
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
              (builtins.sort builtins.lessThan (lib.attrNames profileAgent.config.capabilities))
              == (builtins.sort builtins.lessThan expectedCapabilityNames)
            ) "coding profile must expose the expected capability names"
            && lib.assertMsg (
              minimalAgent.config.build.manifest.capabilities ? read_package_json
            ) "minimal module-authored agent must compile"
            && lib.assertMsg (
              inlineAgent.config.build.manifest.capabilities.read_package_json.grants.packageBins != [ ]
            ) "inline module capability must compile package grants"
            && lib.assertMsg (
              multipleAgents.default.config.build.manifest.capabilities ? read
              && multipleAgents.research.config.build.manifest.capabilities ? web_fetch
            ) "multiple agents under agents.<system> must compile"
            && lib.assertMsg (
              contextAgent.config.build.manifest.context == {
                maxChars = 5000;
                recentTurns = 3;
                autoCompact = true;
              }
              && defaultManifest ? capabilities
              && !(defaultManifest ? context)
            ) "context policy must be emitted only when declared, with set fields only"
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
            ) "shell_escape must stay denied and absent from tools"
            && lib.assertMsg (lib.all evalFails (lib.attrValues schemaFailureAgents)) "malformed capability declarations must fail the module schema"
            && lib.assertMsg (lib.all evalFails (lib.attrValues validationFailureAgents)) "type-valid capabilities that violate a capability rule must fail capabilityAssertions";
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
          default = self.agents.${system}.default.config.build.bundle;
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
