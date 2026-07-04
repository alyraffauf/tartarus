{
  lib,
  nixpkgs,
}:

let
  inherit (lib) types;

  nonEmptyString = types.addCheck types.str (value: value != "");

  paramSchema =
    param:
    {
      inherit (param) type description;
    }
    // lib.optionalAttrs (param.enum != null) { inherit (param) enum; };

  paramsToSchema = params: {
    type = "object";
    properties = lib.mapAttrs (_: paramSchema) params;
    required = lib.attrNames (lib.filterAttrs (_: param: param.required) params);
  };

  toolOf = name: capability: {
    inherit name;
    inherit (capability) description;
    parameters = paramsToSchema capability.params;
  };

  packageBinRoot =
    package:
    let
      binOutput = lib.getBin package;
    in
    if builtins.pathExists (binOutput + "/bin") then binOutput else package;

  # The bin path is always "<root>/bin"; `packageBinRoot` already resolved which
  # store path holds it, so no second probe is needed.
  packageBin = package: "${packageBinRoot package}/bin";

  # `info` is a capability's precomputed grant info: the resolved package roots
  # and the single `closureInfo` derivation built from them (see `tartarusAgent`),
  # so the closure is realized once and shared between the manifest and the bundle.
  grantToJson =
    info: grant:
    builtins.removeAttrs grant [ "packages" ]
    // {
      packageBins = map (root: "${root}/bin") info.roots;
      closure = "${info.closure}/store-paths";
    };

  capabilityToJson =
    info: capability:
    (builtins.removeAttrs capability [ "runner" ])
    // {
      grants = grantToJson info capability.grants;
    }
    // lib.optionalAttrs (capability.runner != null) { inherit (capability) runner; };

  compileManifest =
    grantInfo: capabilities:
    let
      compiledCapabilities = lib.mapAttrs (
        name: capability: capabilityToJson grantInfo.${name} capability
      ) capabilities;
      exposed = lib.filterAttrs (_: capability: capability.policy != "deny") compiledCapabilities;
    in
    {
      tools = lib.mapAttrsToList toolOf exposed;
      capabilities = compiledCapabilities;
    };

  modelType = types.submodule {
    options = {
      provider = lib.mkOption {
        type = types.nullOr nonEmptyString;
        default = null;
      };
      baseUrl = lib.mkOption {
        type = types.nullOr nonEmptyString;
        default = null;
      };
      name = lib.mkOption {
        type = types.nullOr nonEmptyString;
        default = null;
      };
      maxTokens = lib.mkOption {
        type = types.nullOr types.ints.positive;
        default = null;
      };
      sampling = lib.mkOption {
        type = types.nullOr (types.attrsOf types.number);
        default = null;
      };
    };
  };

  contextType = types.submodule {
    options = {
      maxChars = lib.mkOption {
        type = types.nullOr (types.addCheck types.int (n: n >= 0));
        default = null;
        description = "Soft ceiling on effective context size, in characters.";
      };
      recentTurns = lib.mkOption {
        type = types.nullOr (types.addCheck types.int (n: n >= 0));
        default = null;
        description = "Number of recent user turns always kept verbatim.";
      };
      autoCompact = lib.mkOption {
        type = types.nullOr types.bool;
        default = null;
        description = ''
          When true, compact deterministically at a turn boundary once the
          effective context exceeds maxChars. Defaults to off so compaction
          stays an explicit, visible action.
        '';
      };
    };
  };

  paramType = types.submodule {
    options = {
      type = lib.mkOption {
        type = types.enum [
          "string"
          "integer"
          "boolean"
          "array"
        ];
      };
      description = lib.mkOption {
        type = types.str;
        default = "";
      };
      required = lib.mkOption {
        type = types.bool;
        default = false;
      };
      enum = lib.mkOption {
        type = types.nullOr (types.listOf types.anything);
        default = null;
      };
    };
  };

  enumEntryMatchesParam =
    param: entry:
    if param.type == "string" then
      builtins.isString entry
    else if param.type == "integer" then
      builtins.isInt entry && !builtins.isBool entry
    else if param.type == "boolean" then
      builtins.isBool entry
    else if param.type == "array" then
      builtins.isList entry
    else
      false;

  paramEnumMatchesType =
    param: param.enum == null || lib.all (enumEntryMatchesParam param) param.enum;

  runnerPlaceholders =
    runner:
    let
      isPlaceholderStart = char: builtins.match "[A-Za-z_]" char != null;
      isPlaceholderChar = char: builtins.match "[A-Za-z0-9_]" char != null;
      nextChar = chars: if chars == [ ] then null else builtins.head chars;
      remainingChars = chars: if chars == [ ] then [ ] else builtins.tail chars;

      skipUntilClosingBrace =
        chars:
        if chars == [ ] then
          [ ]
        else if builtins.head chars == "}" then
          builtins.tail chars
        else
          skipUntilClosingBrace (builtins.tail chars);

      readPlaceholderName =
        chars: name:
        if chars == [ ] then
          {
            name = null;
            rest = [ ];
          }
        else
          let
            char = builtins.head chars;
            rest = builtins.tail chars;
          in
          if char == "}" then
            {
              inherit name rest;
            }
          else if char == ":" || char == "!" then
            {
              inherit name;
              rest = skipUntilClosingBrace rest;
            }
          else if isPlaceholderChar char then
            readPlaceholderName rest (name + char)
          else
            {
              name = null;
              rest = skipUntilClosingBrace rest;
            };

      readPlaceholder =
        chars:
        if chars == [ ] || !isPlaceholderStart (builtins.head chars) then
          {
            name = null;
            rest = chars;
          }
        else
          readPlaceholderName (builtins.tail chars) (builtins.head chars);

      parse =
        chars:
        if chars == [ ] then
          [ ]
        else
          let
            char = builtins.head chars;
            rest = builtins.tail chars;
            followingChar = nextChar rest;
            afterFollowingChar = remainingChars rest;
          in
          if char == "{" && followingChar == "{" then
            parse afterFollowingChar
          else if char == "}" && followingChar == "}" then
            parse afterFollowingChar
          else if char == "{" then
            let
              placeholder = readPlaceholder rest;
            in
            lib.optional (placeholder.name != null) placeholder.name ++ parse placeholder.rest
          else
            parse rest;
    in
    if runner == null then [ ] else parse (lib.stringToCharacters runner);

  runnerPlaceholdersAreDeclared =
    capability:
    lib.all (placeholder: builtins.hasAttr placeholder capability.params) (
      runnerPlaceholders capability.runner
    );

  grantOpensReach =
    grant:
    grant.packages != [ ]
    || grant.network.allowedHosts != [ ]
    || grant.writable != [ ]
    || grant.unrestricted;

  reservedCapabilityNames = [
    "context_status"
    "context_read"
  ];

  reservedSamplingKeys = [
    "model"
    "max_tokens"
    "messages"
    "stream"
    "tools"
    "tool_choice"
  ];

  capabilityType = {
    options = {
      description = lib.mkOption {
        type = types.str;
        default = "";
      };
      policy = lib.mkOption {
        type = types.enum [
          "auto"
          "ask-once"
          "ask-always"
          "deny"
        ];
      };
      params = lib.mkOption {
        type = types.attrsOf paramType;
        default = { };
      };
      grants = {
        packages = lib.mkOption {
          type = types.listOf types.package;
          default = [ ];
        };
        network.allowedHosts = lib.mkOption {
          type = types.listOf types.str;
          default = [ ];
        };
        writable = lib.mkOption {
          type = types.listOf types.str;
          default = [ ];
        };
        unrestricted = lib.mkOption {
          type = types.bool;
          default = false;
        };
      };
      runner = lib.mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      kind = lib.mkOption {
        type = types.enum [
          "command"
          "background"
          "control"
        ];
        default = "command";
      };
      timeout = lib.mkOption {
        type = types.nullOr types.ints.positive;
        default = null;
      };
      control = lib.mkOption {
        type = types.nullOr (
          types.enum [
            "status"
            "output"
            "stop"
          ]
        );
        default = null;
      };
    };
  };

  # The cross-field rules Nix types cannot express on their own. Returns the full
  # rule list (assertion + message) per capability; the `assertions` option
  # aggregates them and build outputs check them lazily via `assertWarn`.
  # Standard NixOS shape, so downstream modules can contribute their own.
  capabilityAssertions = name: capability: [
    {
      assertion = lib.all paramEnumMatchesType (lib.attrValues capability.params);
      message = "Tartarus capability '${name}' has param enum entries that do not match their declared type.";
    }
    {
      assertion = runnerPlaceholdersAreDeclared capability;
      message = "Tartarus capability '${name}' runner references an undeclared param.";
    }
    {
      assertion = !(lib.elem name reservedCapabilityNames);
      message = "Tartarus capability '${name}' uses a reserved internal tool name.";
    }
    {
      assertion = !(capability.grants.unrestricted && capability.policy == "auto");
      message = "Tartarus capability '${name}' cannot combine unrestricted = true with policy = \"auto\".";
    }
    {
      assertion = capability.kind != "background" || capability.timeout == null;
      message = "Tartarus background capability '${name}' cannot declare timeout.";
    }
    {
      assertion = capability.kind != "background" || !capability.grants.unrestricted;
      message = "Tartarus background capability '${name}' cannot be unrestricted.";
    }
    {
      assertion = capability.kind != "control" || capability.control != null;
      message = "Tartarus control capability '${name}' must declare control.";
    }
    {
      assertion = capability.kind == "control" || capability.control == null;
      message = "Tartarus capability '${name}' can declare control only when kind = \"control\".";
    }
    {
      assertion = capability.kind != "control" || capability.runner == null;
      message = "Tartarus control capability '${name}' must not declare runner.";
    }
    {
      assertion = capability.kind != "control" || !grantOpensReach capability.grants;
      message = "Tartarus control capability '${name}' must not declare grants.";
    }
    {
      assertion = capability.kind == "control" || capability.runner != null;
      message = "Tartarus capability '${name}' must declare runner.";
    }
  ];

  # A trimmed subset of NixOS's `nixpkgs` module: an agent (or any of its
  # modules) configures its package set declaratively, and every module receives
  # the result as `pkgs` via `_module.args`.
  nixpkgsModule =
    { config, ... }:
    let
      cfg = config.nixpkgs;
    in
    {
      options.nixpkgs = {
        hostPlatform = lib.mkOption {
          type = types.str;
        };
        config = lib.mkOption {
          type = types.attrs;
          default = { };
        };
        overlays = lib.mkOption {
          type = types.listOf (
            lib.mkOptionType {
              name = "nixpkgs-overlay";
              description = "nixpkgs overlay";
              check = lib.isFunction;
              merge = lib.mergeOneOption;
            }
          );
          default = [ ];
        };
        pkgs = lib.mkOption {
          type = types.raw;
          # Reuse the flake's memoized legacyPackages when nothing is customized
          # (cheap, shared across the flake); otherwise import per config/overlays.
          default =
            if cfg.config == { } && cfg.overlays == [ ] then
              nixpkgs.legacyPackages.${cfg.hostPlatform}
            else
              import nixpkgs {
                localSystem = cfg.hostPlatform;
                inherit (cfg) config overlays;
              };
        };
      };

      config._module.args.pkgs = cfg.pkgs;
    };

  shellEnvReservedNames = [
    "BASH_ENV"
    "HOME"
    "LANG"
    "LC_ALL"
    "PATH"
    "SSL_CERT_FILE"
    "NIX_SSL_CERT_FILE"
    "CURL_CA_BUNDLE"
    "REQUESTS_CA_BUNDLE"
  ];

  isValidShellEnvName =
    name:
    let
      upper = lib.toUpper name;
    in
    builtins.match "^[A-Za-z_][A-Za-z0-9_]*$" name != null
    && !(lib.elem upper shellEnvReservedNames)
    && !(lib.hasSuffix "_PROXY" upper)
    && !(lib.hasPrefix "TARTARUS_" upper);

  modelAssertions =
    model:
    lib.optionals (model != null) [
      {
        assertion =
          model.sampling == null
          || lib.intersectLists reservedSamplingKeys (lib.attrNames model.sampling) == [ ];
        message = "Tartarus model.sampling contains reserved request body keys.";
      }
    ];

  compileAgentManifest =
    {
      capabilities,
      config,
      grantInfo,
      hookDrv,
      pkgs,
      shellClosureDrv,
      shellRootList,
    }:
    compileManifest grantInfo capabilities
    // {
      caBundle = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
      shellClosure = "${shellClosureDrv}/store-paths";
      shellPath = lib.concatStringsSep ":" (lib.unique (map (root: "${root}/bin") shellRootList));
      shellEnv = config.shell.env;
    }
    // lib.optionalAttrs (config.systemPrompt != null) { inherit (config) systemPrompt; }
    // lib.optionalAttrs (config.model != null) { inherit (config) model; }
    // lib.optionalAttrs (config.context != null) {
      # Emit only the fields the agent set; null fields stay absent so the
      # harness applies env override or built-in default (resolve_context).
      context = lib.filterAttrs (_: value: value != null) config.context;
    }
    // lib.optionalAttrs (hookDrv != null) { shellHook = "${hookDrv}"; };

  buildAgentBundle =
    {
      config,
      grantInfo,
      manifest,
      pkgs,
      shellClosureDrv,
    }:
    pkgs.runCommand "tartarus-${config.name}-bundle"
      {
        manifestJson = builtins.toJSON manifest;
        passAsFile = [ "manifestJson" ];
        grantClosures = map (info: info.closure) (lib.attrValues grantInfo);
      }
      ''
        mkdir -p "$out/closures"
        cp "$manifestJsonPath" "$out/manifest.json"
        ln -s ${shellClosureDrv} "$out/closures/shell"
        n=0
        for closure in $grantClosures; do
          ln -s "$closure" "$out/closures/grant-$n"
          n=$((n + 1))
        done
      '';

  agentModule =
    { pkgs, config, ... }:
    {
      options = {
        # The agent's identity: names the bundle derivation, mirroring how
        # `config.system.name` (← `networking.hostName`) names a NixOS toplevel.
        # Defaults to a constant; set it per agent for descriptive, non-colliding
        # bundle labels across multi-agent flakes.
        name = lib.mkOption {
          type = types.str;
          default = "agent";
        };
        systemPrompt = lib.mkOption {
          type = types.nullOr types.str;
          default = null;
        };
        model = lib.mkOption {
          type = types.nullOr modelType;
          default = null;
        };
        context = lib.mkOption {
          type = types.nullOr contextType;
          default = null;
          description = "Optional context policy; null fields fall back to harness defaults.";
        };
        shell = {
          packages = lib.mkOption {
            type = types.listOf types.package;
            default = with pkgs; [
              bash
              coreutils
            ];
            description = "Packages whose /bin directories form the agent's baseline PATH.";
          };
          env = lib.mkOption {
            type = types.attrsOf types.str;
            default = { };
            description = ''
              Extra environment variables exposed to every jailed call.
              Reserved names (PATH, HOME, locale vars, cert vars, proxy vars,
              BASH_ENV, and names beginning with TARTARUS_) are rejected at
              build time.
            '';
          };
          hook = lib.mkOption {
            type = types.nullOr types.str;
            default = null;
            description = ''
              Optional bash script sourced by every jailed call via BASH_ENV.
              It runs after jail setup — the closure is bound, PATH is composed
              (shell PATH plus the call's grant bins), and shell.env is exported —
              so it can reach any tool the command itself can. It must be
              idempotent: a command that is itself `bash -c …` re-sources it.
            '';
          };
        };
        capabilities = lib.mkOption {
          type = types.attrsOf (types.submodule capabilityType);
          default = { };
        };
        assertions = lib.mkOption {
          type = types.listOf (
            types.submodule {
              options = {
                assertion = lib.mkOption { type = types.bool; };
                message = lib.mkOption { type = types.str; };
              };
            }
          );
          default = [ ];
          internal = true;
        };
        warnings = lib.mkOption {
          type = types.listOf types.str;
          default = [ ];
          internal = true;
        };
      };

      config.assertions =
        lib.concatLists (lib.mapAttrsToList capabilityAssertions config.capabilities)
        ++ modelAssertions config.model;
    };

  # The agent's build outputs, living in the module graph at `config.build.*` —
  # the agent analog of NixOS's `config.system.build.toplevel`. Assertions and
  # warnings are evaluated NixOS-style when a build output is forced: reading
  # `config` is free, but realizing the manifest or bundle checks the contract.
  buildModule =
    { config, pkgs, ... }:
    let
      capabilities = config.capabilities;
      # One `closureInfo` per capability, shared between the manifest `closure`
      # pointer and the bundle's symlinks. `roots` is reused for `packageBins`.
      grantInfo = lib.mapAttrs (
        _: capability:
        let
          roots = map packageBinRoot capability.grants.packages;
        in
        {
          inherit roots;
          closure = pkgs.closureInfo { rootPaths = roots; };
        }
      ) capabilities;
      shellBinPackages = config.shell.packages ++ [ pkgs.bashInteractive ];
      shellRootList = map packageBinRoot shellBinPackages;
      hookDrv = lib.mapNullable (pkgs.writeText "tartarus-shell-hook") config.shell.hook;
      shellRoots = shellRootList ++ [ pkgs.cacert ] ++ lib.optional (hookDrv != null) hookDrv;
      shellClosureDrv = pkgs.closureInfo { rootPaths = shellRoots; };
      compiledManifest = compileAgentManifest {
        inherit
          capabilities
          config
          grantInfo
          hookDrv
          pkgs
          shellClosureDrv
          shellRootList
          ;
      };
      assertWarn =
        result:
        let
          failed = lib.filter (assertion: !assertion.assertion) config.assertions;
        in
        if failed != [ ] then
          throw (
            "Tartarus agent assertion failures:\n"
            + lib.concatMapStringsSep "\n" (assertion: "  - ${assertion.message}") failed
          )
        else
          lib.showWarnings config.warnings result;
    in
    {
      options.build = {
        manifest = lib.mkOption {
          type = types.raw;
          readOnly = true;
        };
        bundle = lib.mkOption {
          type = types.package;
          readOnly = true;
        };
        shell = lib.mkOption {
          type = types.package;
          readOnly = true;
        };
      };

      config.assertions = [
        {
          assertion = lib.all isValidShellEnvName (lib.attrNames config.shell.env);
          message = "Tartarus shell.env contains invalid or reserved variable names.";
        }
      ];

      config.build = {
        manifest = assertWarn compiledManifest;

        shell = assertWarn (
          pkgs.mkShellNoCC {
            packages = config.shell.packages;
            env = config.shell.env;
            shellHook = lib.optionalString (config.shell.hook != null) config.shell.hook;
          }
        );

        bundle = assertWarn (buildAgentBundle {
          inherit
            config
            grantInfo
            pkgs
            shellClosureDrv
            ;
          manifest = compiledManifest;
        });
      };
    };

  # Evaluate an agent's module graph. Returns the `lib.evalModules` result —
  # `config`, `options`, `extendModules`, `class`, `type`, `_module` — plus
  # `pkgs`, mirroring `nixpkgs.lib.nixosSystem`. Build outputs live at
  # `result.config.build.{manifest,bundle,shell}`.
  tartarusAgent =
    {
      system,
      modules,
      specialArgs ? { },
    }:
    let
      evaluated = lib.evalModules {
        class = "tartarus";
        inherit specialArgs;
        modules = [
          agentModule
          nixpkgsModule
          buildModule
          { nixpkgs.hostPlatform = lib.mkDefault system; }
        ]
        ++ modules;
      };
    in
    evaluated // { pkgs = evaluated.config.nixpkgs.pkgs; };

  evalAgentConfig = args: (tartarusAgent args).config;
in
{
  inherit tartarusAgent evalAgentConfig;
}
