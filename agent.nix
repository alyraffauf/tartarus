# The example agent. One agent named `default`, whose `capabilities` is a plain
# list of capability specs and capability modules. Each spec self-identifies via
# `name`; the lib keys them by it. Common coding-agent tools come from
# `agentModules`; showcase-specific tools stay inline. The lib accepts both
# plain attrsets and module functions. To add a second agent, add another named
# entry alongside `default`.

{ pkgs, agentModules }:

{
  default = {
    # The agent's model: the backend a model id is only meaningful within
    # (`baseUrl`, `name`, optional `provider` type) plus its inference knobs
    # (`maxTokens`, `sampling`). Optional — omit to inherit the harness defaults.
    # API keys and request headers are never declared here; they stay in the
    # environment (TARTARUS_API_KEY / OPENCODE_API_KEY, TARTARUS_EXTRA_HEADERS). A
    # set env var still overrides these, so the same agent can be pointed at a
    # different backend without editing the flake.
    model = {
      baseUrl = "https://opencode.ai/zen/v1";
      name = "glm-5.2";
      # Generous completion budget for multi-file edits (GLM serves up to 128k
      # output); temperature 0.6 keeps coding focused without going robotic.
      maxTokens = 32768;
      sampling = {
        temperature = 0.6;
      };
    };

    # The agent's shell: the baseline PATH every jailed tool call starts with,
    # before per-capability grants are layered on. Declared inline here as a plain
    # package list, which the lib wraps into a devShell. Omit `shell` entirely to
    # fall back to the minimal default (bash + coreutils); set it to an existing
    # devShell derivation to reuse one instead of declaring it in-line. Capabilities
    # still carry their own `grants.packages`, so keep this lean.
    shell = with pkgs; [
      bash
      coreutils
    ];

    # The agent's persona. Omit to fall back to the harness default.
    systemPrompt = ''
      You are an agent operating inside Tartarus, a capability-brokered
      environment. Your tools are capabilities: each is a declared, auditable
      reach beyond your sealed workspace, such as running a command, reading or
      writing a path, or contacting a host. Use them whenever they serve the
      user's goal. Coding is one domain among many; reading and reasoning over
      data, searching, fetching information, and producing artifacts are all
      first-class.

      You run in a "shell" that holds only the tools declared for you: nothing
      from the host, no ambient network, no filesystem beyond your work tree. Each
      capability carries a policy. Some run automatically, some need the human to
      approve the exact access first, some are denied. Treat approvals and denials
      as normal; when denied, adapt instead of insisting. Binaries and network
      access are granted only for the single call that needs them, so never call a
      tool's packages permanently installed, and never assume reach you were not
      given.

      Be precise and honest about what you did and what you could not. Reach for
      the narrowest capability that does the job.
    '';

    capabilities = [
      # Read-only work-tree introspection. These run automatically because they
      # only read /work and have no network or writable grants.
      agentModules.read
      agentModules.glob
      agentModules.list
      agentModules.grep

      {
        name = "jq";
        description = "Run a jq query against a JSON file in the work tree.";
        policy = "auto";
        params = {
          path = {
            type = "string";
            description = "JSON file to query, relative to the work tree.";
            required = true;
            enum = null;
          };
          filter = {
            type = "string";
            description = "jq filter expression, such as '.dependencies'.";
            required = true;
            enum = null;
          };
        };
        grants = {
          packages = [ pkgs.jq ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "jq {filter} {path}";
      }

      # Repository state is read-only but high-value for coding agents.
      {
        name = "git_status";
        description = "Show concise Git working tree status.";
        policy = "auto";
        params = { };
        grants = {
          packages = [ pkgs.git ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "git status --short";
      }

      {
        name = "git_diff";
        description = "Show unstaged Git diff for the whole work tree or one path.";
        policy = "auto";
        params.path = {
          type = "string";
          description = "Optional path to diff, relative to the work tree.";
          required = false;
          enum = null;
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.git
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "bash -c 'if [ -n \"$1\" ]; then git diff -- \"$1\"; else git diff; fi' _ {path}";
      }

      {
        name = "git_log";
        description = "Show recent Git commits, optionally limited to one path.";
        policy = "auto";
        params = {
          limit = {
            type = "integer";
            description = "Maximum number of commits to show. Defaults to 20.";
            required = false;
            enum = null;
          };
          path = {
            type = "string";
            description = "Optional path to limit history to, relative to the work tree.";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.git
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "bash -c 'limit=$1; path=$2; if [ -z \"$limit\" ]; then limit=20; fi; if [ -n \"$path\" ]; then git log --oneline -n \"$limit\" -- \"$path\"; else git log --oneline -n \"$limit\"; fi' _ {limit} {path}";
      }

      {
        name = "git_show";
        description = "Show one Git revision or a file from one revision.";
        policy = "auto";
        params = {
          revision = {
            type = "string";
            description = "Git revision to inspect. Defaults to HEAD.";
            required = false;
            enum = null;
          };
          path = {
            type = "string";
            description = "Optional file path to show from the revision.";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.git
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "bash -c 'revision=$1; path=$2; if [ -z \"$revision\" ]; then revision=HEAD; fi; if [ -n \"$path\" ]; then git show --end-of-options \"$revision:$path\"; else git show --stat --patch --end-of-options \"$revision\"; fi' _ {revision} {path}";
      }

      # Work-tree mutation. These are ask-once so routine edit loops stay
      # ergonomic while the human still approves write access per session.
      agentModules.write
      agentModules.edit

      # General command execution inside the shell. This is still jailed and
      # networkless, but arbitrary shell commands deserve per-call approval.
      agentModules.bash

      # Long-running work that should not block the turn. `kind = "background"`
      # launches the command detached and returns a handle (such as bg-1)
      # immediately; the control capabilities below inspect and stop it, and a
      # completion notice is injected into the conversation when it exits.
      {
        name = "background_bash";
        description = ''
          Start a shell command running in the background and return a task
          handle (such as bg-1) right away, without waiting for it to finish.
          Use bg_status and bg_output to follow it and bg_stop to end it. The
          work tree is writable; there is no network.
        '';
        policy = "ask-always";
        kind = "background";
        params.command = {
          type = "string";
          description = "The command line to run detached inside the jail.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.bash ];
          network.allowedHosts = [ ];
          writable = [ "." ];
          unrestricted = false;
        };
        runner = "bash -c {command}";
      }

      # Control-plane tools. `kind = "control"` capabilities act on the background
      # registry rather than the jail, so they carry no runner and no grants.
      {
        name = "bg_status";
        description = "Report whether a background task is still running, or its exit code.";
        policy = "auto";
        kind = "control";
        control = "status";
        params.task = {
          type = "string";
          description = "Background task handle, such as bg-1.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "";
      }

      {
        name = "bg_output";
        description = "Read the accumulated stdout/stderr of a background task.";
        policy = "auto";
        kind = "control";
        control = "output";
        params = {
          task = {
            type = "string";
            description = "Background task handle, such as bg-1.";
            required = true;
            enum = null;
          };
          offset = {
            type = "integer";
            description = "Byte offset to read from. Defaults to 0 (the whole log).";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [ ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "";
      }

      {
        name = "bg_stop";
        description = "Stop a running background task by signalling its process group.";
        policy = "ask-once";
        kind = "control";
        control = "stop";
        params.task = {
          type = "string";
          description = "Background task handle, such as bg-1.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "";
      }

      # Formats all .nix files in the work tree. It rewrites files, so it is
      # gated with ask-once: the human approves write access once per session,
      # then nixfmt runs freely inside the writable "." grant.
      {
        name = "format_nix";
        description = "Format Nix files in the work tree with nixfmt.";
        policy = "ask-once";
        params = { };
        grants = {
          packages = [
            pkgs.bash
            pkgs.findutils
            pkgs.nixfmt
          ];
          network.allowedHosts = [ ];
          writable = [ "." ];
          unrestricted = false;
        };
        runner = "bash -c 'find . -name \"*.nix\" -print0 | xargs -0 nixfmt'";
      }

      {
        name = "pytest";
        description = "Run the project test suite without network access.";
        policy = "ask-once";
        # Capabilities run unbounded by default; a full test run is the rare case
        # that wants a ceiling, so it caps itself at five minutes. Omit `timeout`
        # to let a capability run without a limit.
        timeout = 300;
        params.filter = {
          type = "string";
          description = "Optional pytest filter expression.";
          required = false;
          enum = null;
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.python3Packages.pytest
          ];
          network.allowedHosts = [ ];
          writable = [ "." ];
          unrestricted = false;
        };
        runner = "bash -c 'if [ -n \"$1\" ]; then pytest -k \"$1\"; else pytest; fi' _ {filter}";
      }

      # Narrow artifact output. This demonstrates granting a specific writable
      # subdirectory instead of making the whole work tree writable.
      {
        name = "write_artifact";
        description = "Write a file under the work tree's artifacts directory.";
        policy = "ask-always";
        params = {
          path = {
            type = "string";
            description = "Artifact path under the configured artifacts directory.";
            required = true;
            enum = null;
          };
          content = {
            type = "string";
            description = "Complete artifact content.";
            required = true;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.coreutils
          ];
          network.allowedHosts = [ ];
          writable = [ "artifacts" ];
          unrestricted = false;
        };
        runner = ''
          bash -c 'case "$1" in /*|*..*) echo "artifact path must stay under artifacts" >&2; exit 2;; esac; mkdir -p artifacts "$(dirname "artifacts/$1")"; printf %s "$2" > "artifacts/$1"' _ {path} {content}
        '';
      }

      # Scoped HTTP egress through the filtering proxy.
      {
        name = "pypi_versions";
        description = "Query Python package versions through the scoped HTTP proxy.";
        policy = "ask-once";
        params.package = {
          type = "string";
          description = "Python package name or requirement to inspect.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.python3Packages.pip ];
          network.allowedHosts = [
            "pypi.org:443"
          ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "pip --no-cache-dir index versions {package}";
      }

      {
        name = "fetch_rfc";
        description = "Fetch a plain-text RFC from rfc-editor.org through the scoped HTTP proxy.";
        policy = "auto";
        params.number = {
          type = "integer";
          description = "RFC number to fetch.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.curl ];
          network.allowedHosts = [ "www.rfc-editor.org:443" ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "curl -fsSL https://www.rfc-editor.org/rfc/rfc{number}.txt";
      }

      # Wildcard HTTP egress is useful for research, but always prompt and
      # audit the actual destination reported by the proxy.
      agentModules.web_fetch

      # The big red button. Trusted overlays may flip this to ask-always; the
      # manifest validator rejects unrestricted + auto.
      {
        name = "shell_escape";
        description = "Disabled unrestricted host escape for trusted overlays only.";
        policy = "deny";
        params = { };
        grants = {
          packages = [ ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = true;
        };
        runner = "bash";
      }
    ];
  };
}
