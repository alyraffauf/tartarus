{ lib }:

let
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

  packageBin =
    package:
    let
      binRoot = packageBinRoot package;
      hasBinDir = builtins.pathExists (binRoot + "/bin");
    in
    if hasBinDir then "${binRoot}/bin" else "${package}/bin";

  packageBinRoot =
    package:
    let
      binOutput = lib.getBin package;
    in
    if builtins.pathExists (binOutput + "/bin") then binOutput else package;

  # The closure of a grant's packages, emitted as a store path to the
  # newline-list `closureInfo` produces. The harness binds exactly these paths
  # into the jail, so a capability reaches its declared closure and nothing else.
  # A store path string, not IFD: the file is realized by the `grantClosures`
  # build and read by the harness afterward, never during eval.
  closureFile = pkgs: roots: "${pkgs.closureInfo { rootPaths = roots; }}/store-paths";

  grantToJson =
    pkgs: grant:
    let
      packageRoots = map packageBinRoot (grant.packages or [ ]);
    in
    builtins.removeAttrs grant [ "packages" ]
    // {
      packageBins = map packageBin (grant.packages or [ ]);
      closure = closureFile pkgs packageRoots;
    };

  capabilityToJson =
    pkgs: capability:
    capability
    // {
      grants = grantToJson pkgs capability.grants;
    };

  compileManifest =
    pkgs: capabilities:
    let
      compiledCapabilities = lib.mapAttrs (_: capability: capabilityToJson pkgs capability) capabilities;
      exposed = lib.filterAttrs (_: capability: capability.policy != "deny") compiledCapabilities;
    in
    {
      tools = lib.mapAttrsToList toolOf exposed;
      capabilities = compiledCapabilities;
    };

  # A capability self-identifies via `name`. Accept either a plain attrset (when
  # `pkgs` is already in scope) or a function of `moduleArgs` (to share it across
  # flakes). `name` is stripped from the body because the attr key carries it.
  resolveCapabilities =
    moduleArgs: capabilityModules:
    lib.foldl' (
      resolved: capabilityModule:
      let
        capability =
          if lib.isFunction capabilityModule then capabilityModule moduleArgs else capabilityModule;
        name = capability.name or (throw "Tartarus: a capability is missing its `name`");
      in
      if resolved ? ${name} then
        throw "Tartarus: duplicate capability name '${name}'"
      else
        resolved // { ${name} = builtins.removeAttrs capability [ "name" ]; }
    ) { } capabilityModules;

  # The default shell: the always-present baseline PATH inside every jailed call,
  # before any capability grant is layered on. Kept deliberately minimal so the
  # shell reflects the capability-OS model — each tool brings its own packages via
  # `grants.packages`. Agents that want a richer baseline declare their own `shell`.
  defaultShellPackages = pkgs: [
    pkgs.bash
    pkgs.coreutils
  ];

  # An agent's `shell` may be omitted (use the minimal default), given as a plain
  # list of packages (wrapped into a shell here), or given as a devShell
  # derivation directly (reused from the flake or declared inline). Its PATH is
  # baked into the bundle manifest; the shell output remains useful for humans.
  resolveShell =
    pkgs: shell:
    if shell == null then
      pkgs.mkShellNoCC { packages = defaultShellPackages pkgs; }
    else if lib.isList shell then
      pkgs.mkShellNoCC { packages = shell; }
    else
      shell;

  # The packages whose closure must be bound for the baseline shell PATH to work
  # inside the jail. For the list and default forms we know the packages exactly;
  # for a devShell-derivation `shell` we fall back to its inputs (so a custom
  # devShell must declare its runtime PATH deps as packages — by design).
  shellPackagesOf =
    pkgs: shell:
    if shell == null then
      defaultShellPackages pkgs
    else if lib.isList shell then
      shell
    else
      (shell.buildInputs or [ ]) ++ (shell.nativeBuildInputs or [ ]);

  mkAgent =
    moduleArgs:
    {
      capabilities,
      systemPrompt ? null,
      shell ? null,
      grantEnvName ? "tartarus-nix-grants",
      # The agent's model: one coherent unit holding the backend a model id is
      # only meaningful within (`provider` type, `baseUrl`, `name`) plus its
      # inference knobs (`maxTokens`, `sampling`). Optional — an agent that omits
      # it inherits the harness defaults (PLAN.md §9). API keys and request
      # headers are never declared here: they stay in the environment.
      model ? null,
    }:
    let
      resolved = resolveCapabilities moduleArgs capabilities;
      pkgs = moduleArgs.pkgs;
      # The baseline PATH packages: the declared shell plus bashInteractive (the
      # shell `nix develop` used to run). The baked `shellPath` and the bound
      # `shellClosure` share these roots, so PATH never advertises a binary the
      # jail does not bind. cacert carries no bin; it rides the closure only, for
      # the CA bundle so TLS works inside the jail for network grants.
      shellBinPackages = shellPackagesOf pkgs shell ++ [ pkgs.bashInteractive ];
      shellRoots = map packageBinRoot shellBinPackages ++ [ pkgs.cacert ];
      shellClosureDrv = pkgs.closureInfo { rootPaths = shellRoots; };
      grantClosureDrvs = map (
        capability:
        pkgs.closureInfo {
          rootPaths = map packageBinRoot (capability.grants.packages or [ ]);
        }
      ) (lib.attrValues resolved);

      # The compiled, fully-resolved manifest: tool/capability contract plus the
      # baked baseline PATH, the shell closure pointer, the CA bundle, and the
      # optional persona/model. Serialized verbatim into the bundle below.
      compiledManifest =
        compileManifest pkgs resolved
        // {
          caBundle = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
          shellClosure = "${shellClosureDrv}/store-paths";
          shellPath = lib.concatStringsSep ":" (lib.unique (map packageBin shellBinPackages));
        }
        // lib.optionalAttrs (systemPrompt != null) { inherit systemPrompt; }
        // lib.optionalAttrs (model != null) { inherit model; };
    in
    {
      capabilities = resolved;

      # The agent owns its shell: a devShell kept for `nix develop` ergonomics.
      # The harness no longer resolves it — the baseline PATH is baked into the
      # manifest's `shellPath` — but it stays a convenient entry point.
      shell = resolveShell pkgs shell;

      manifest = compiledManifest;

      # The shippable agent: one derivation whose runtime closure is the whole
      # agent. Writing the manifest JSON into $out makes the output reference
      # every store path it names (package bins, each grant's `closure`
      # store-paths file, the shell closure, the CA bundle, the baked PATH
      # entries), so `nix copy <bundle>` pulls the complete closure. The symlinks
      # force realization and aid debugging. The harness reads
      # <bundle>/manifest.json with no nix calls (tartarus/bundle.py).
      bundle =
        pkgs.runCommand "${grantEnvName}-bundle"
          {
            manifestJson = builtins.toJSON compiledManifest;
            passAsFile = [ "manifestJson" ];
            closures = [ shellClosureDrv ] ++ grantClosureDrvs;
          }
          ''
            mkdir -p "$out/closures"
            cp "$manifestJsonPath" "$out/manifest.json"
            ln -s ${shellClosureDrv} "$out/closures/shell"
            n=0
            for closure in $closures; do
              ln -s "$closure" "$out/closures/grant-$n"
              n=$((n + 1))
            done
          '';
    };
in
{
  inherit mkAgent resolveCapabilities;

  mkAgents =
    moduleArgs: agents:
    lib.mapAttrs (
      agentName: agentConfig:
      mkAgent moduleArgs (
        agentConfig
        // {
          grantEnvName = agentConfig.grantEnvName or "tartarus-nix-${agentName}-grants";
        }
      )
    ) agents;
}
