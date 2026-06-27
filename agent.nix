# The example agent. One agent named `default`, whose `capabilities` is a plain
# list of capability specs and capability modules. Each spec self-identifies via
# `name`; the lib keys them by it. `count_lines` and `edit_file` are written as
# modules — functions of `{ pkgs, packages, ... }` — so they can be lifted out and
# shared across flakes. The rest are plain attrsets because `pkgs` is already in
# scope here. The lib accepts both forms. To add a second agent, add another named
# entry alongside `default`.

{ pkgs }:

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
      # A self-contained module: it defines its own helper derivation from the
      # passed-in `pkgs`, so the whole capability can be copied into another flake.
      (
        { pkgs, ... }:
        let
          count-lines = pkgs.writeShellApplication {
            name = "count-lines";
            runtimeInputs = [
              pkgs.coreutils
              pkgs.findutils
            ];
            text = ''
              target="''${1:-.}"
              if [ -d "$target" ]; then
                find "$target" -type f \
                  -not -path './.git/*' \
                  -not -path './.tartarus/*' \
                  -not -path './.direnv/*' \
                  -print0 |
                  xargs -0 wc -l |
                  tail -n 1
              else
                wc -l < "$target"
              fi
            '';
          };
        in
        {
          name = "count_lines";
          description = "Count source lines with a tiny package exported by this flake.";
          policy = "auto";
          params.path = {
            type = "string";
            description = "File or directory to count, relative to the work tree. Defaults to '.'.";
            required = false;
            enum = null;
          };
          grants = {
            packages = [
              pkgs.bash
              count-lines
            ];
            network.allowedHosts = [ ];
            writable = [ ];
            unrestricted = false;
          };
          runner = "bash -c 'path=$1; if [ -z \"$path\" ]; then count-lines; else count-lines \"$path\"; fi' _ {path}";
        }
      )

      # Read-only work-tree introspection. These run automatically because they
      # only read /work and have no network, writable path, or package grants.
      {
        name = "read_file";
        description = "Read a file in the work tree, optionally limited by line range.";
        policy = "auto";
        params = {
          path = {
            type = "string";
            description = "Path to read, relative to the work tree.";
            required = true;
            enum = null;
          };
          start_line = {
            type = "integer";
            description = "First line to read, 1-based. Defaults to 1.";
            required = false;
            enum = null;
          };
          end_line = {
            type = "integer";
            description = "Last line to read, inclusive. Defaults to the end of the file.";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.gnused
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = ''
          bash -c 'start=$1; end=$2; if [ -z "$start" ]; then start=1; fi; range="$start,\$"; if [ -n "$end" ]; then range="$start,$end"; fi; sed -n "$range"p "$3"' _ {start_line} {end_line} {path}
        '';
      }

      {
        name = "list_dir";
        description = "List a directory in the work tree.";
        policy = "auto";
        params.path = {
          type = "string";
          description = "Directory to list, relative to the work tree. Defaults to '.'.";
          required = false;
          enum = null;
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.coreutils
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "bash -c 'path=$1; if [ -z \"$path\" ]; then path=.; fi; ls -la \"$path\"' _ {path}";
      }

      {
        name = "search";
        description = "Search file contents in the work tree with ripgrep.";
        policy = "auto";
        params = {
          pattern = {
            type = "string";
            description = "Regex pattern to search for.";
            required = true;
            enum = null;
          };
          path = {
            type = "string";
            description = "Path to search, relative to the work tree. Defaults to '.'.";
            required = false;
            enum = null;
          };
          glob = {
            type = "string";
            description = "Optional ripgrep glob filter.";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.bash
            pkgs.ripgrep
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = ''
          bash -c 'target=$2; if [ -z "$target" ]; then target=.; fi; if [ -n "$3" ]; then rg --glob "$3" "$1" "$target"; else rg "$1" "$target"; fi' _ {pattern} {path} {glob}
        '';
      }

      {
        name = "read_json";
        description = "Read and validate a JSON file in the work tree, printing formatted JSON.";
        policy = "auto";
        params.path = {
          type = "string";
          description = "JSON file to read, relative to the work tree.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.python3 ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "python3 -m json.tool {path}";
      }

      {
        name = "query_json";
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
      {
        name = "write_file";
        description = "Create or overwrite a file in the work tree.";
        policy = "ask-once";
        params = {
          path = {
            type = "string";
            description = "Path to write, relative to the work tree.";
            required = true;
            enum = null;
          };
          content = {
            type = "string";
            description = "Complete file content.";
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
          writable = [ "." ];
          unrestricted = false;
        };
        runner = ''
          bash -c 'mkdir -p "$(dirname "$1")"; printf %s "$2" > "$1"' _ {path} {content}
        '';
      }

      # Like count_lines, this capability defines its own helper derivation so the
      # edit logic lives in a readable, testable script rather than a shell
      # one-liner, while staying self-contained for copying into another flake.
      (
        { pkgs, ... }:
        let
          edit-file = pkgs.writers.writePython3Bin "edit-file" { flakeIgnore = [ "E501" ]; } ''
            import pathlib
            import sys

            path = pathlib.Path(sys.argv[1])
            old_text = sys.argv[2]
            new_text = sys.argv[3]
            replace_all = sys.argv[4] == "True"

            text = path.read_text()
            count = text.count(old_text)

            if count == 0:
                sys.exit("no occurrence of old_str found")

            # Require an unambiguous target unless the caller opts into a sweep.
            # Reporting each match's line lets the model widen old_str instead of
            # falling back to a whole-file write_file.
            if not replace_all and count != 1:
                lines = [
                    text.count("\n", 0, i) + 1
                    for i in range(len(text))
                    if text.startswith(old_text, i)
                ]
                locations = ", ".join(str(line) for line in lines)
                sys.exit(
                    f"expected exactly one match, found {count} at lines "
                    f"{locations}; add surrounding context or set replace_all"
                )

            replacements = count if replace_all else 1
            path.write_text(text.replace(old_text, new_text, replacements))
            print(f"replaced {replacements} occurrence(s) in {path}")
          '';
        in
        {
          name = "edit_file";
          description = "Replace an exact string in a work-tree file. Defaults to requiring a single match; set replace_all to substitute every occurrence. Reports how many occurrences were replaced.";
          policy = "ask-once";
          params = {
            path = {
              type = "string";
              description = "Path to edit, relative to the work tree.";
              required = true;
              enum = null;
            };
            old_str = {
              type = "string";
              description = "Exact text to replace. Unless replace_all is set, it must appear exactly once.";
              required = true;
              enum = null;
            };
            new_str = {
              type = "string";
              description = "Replacement text.";
              required = true;
              enum = null;
            };
            replace_all = {
              type = "boolean";
              description = "Replace every occurrence instead of requiring exactly one. Defaults to false.";
              required = false;
              enum = null;
            };
          };
          grants = {
            packages = [ edit-file ];
            network.allowedHosts = [ ];
            writable = [ "." ];
            unrestricted = false;
          };
          runner = "edit-file {path} {old_str} {new_str} {replace_all}";
        }
      )

      # General command execution inside the shell. This is still jailed and
      # networkless, but arbitrary shell commands deserve per-call approval.
      {
        name = "run_command";
        description = ''
          Run a shell command using only tools available in the shell. The work
          tree is writable and there is no network. Git is available for local
          repository inspection. Each call requires approval.
        '';
        policy = "ask-always";
        params.command = {
          type = "string";
          description = "The command line to run inside the jail.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [
            pkgs.bash
          ];
          network.allowedHosts = [ ];
          writable = [ "." ];
          unrestricted = false;
        };
        runner = "bash -c {command}";
      }

      # Long-running work that should not block the turn. `kind = "background"`
      # launches the command detached and returns a handle (such as bg-1)
      # immediately; the control capabilities below inspect and stop it, and a
      # completion notice is injected into the conversation when it exits.
      {
        name = "run_background_command";
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

      {
        name = "format_code";
        description = "Format Nix files in the work tree with nixfmt.";
        policy = "auto";
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
        name = "run_tests";
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

      # Per-call package expansion. The package bins are appended only for this
      # invocation; they do not become part of the permanent shell.
      {
        name = "run_ephemeral_command";
        description = "Run a shell command with allow-listed Nix package binaries available for this jailed call only.";
        policy = "ask-always";
        params = {
          package = {
            type = "string";
            description = "Allow-listed package the command intends to use.";
            required = true;
            enum = [
              "shellcheck"
              "tree"
            ];
          };
          command = {
            type = "string";
            description = "Command line to run with the ephemeral package set on PATH.";
            required = true;
            enum = null;
          };
        };
        grants = {
          packages = [
            pkgs.shellcheck
            pkgs.tree
          ];
          network.allowedHosts = [ ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "bash -c {command}";
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
        name = "fetch_dependency";
        description = "Query Python package metadata through the scoped HTTP proxy.";
        policy = "ask-once";
        params.package = {
          type = "string";
          description = "Package requirement to fetch.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.python3Packages.pip ];
          network.allowedHosts = [
            "pypi.org:443"
            "files.pythonhosted.org:443"
          ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "pip --no-cache-dir index versions {package}";
      }

      # Pre-approved fetch for Wikipedia. The host enum and network allow-list are
      # the same boundary, so this can be auto without opening general web egress.
      {
        name = "fetch_wikipedia";
        description = "Fetch from Wikipedia through the scoped HTTP proxy.";
        policy = "auto";
        params = {
          host = {
            type = "string";
            description = "Allow-listed host to fetch.";
            required = true;
            enum = [
              "en.wikipedia.org"
              "wikipedia.org"
            ];
          };
          path = {
            type = "string";
            description = "URL path beginning with '/'.";
            required = true;
            enum = null;
          };
        };
        grants = {
          packages = [ pkgs.curl ];
          network.allowedHosts = [
            "en.wikipedia.org:443"
            "wikipedia.org:443"
          ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "curl -fsSL https://{host}{path}";
      }

      # Wildcard HTTP egress is useful for research, but always prompt and
      # audit the actual destination reported by the proxy.
      {
        name = "fetch_any_url";
        description = "Fetch any HTTP(S) URL through the scoped HTTP proxy after per-call approval.";
        policy = "ask-always";
        params.url = {
          type = "string";
          description = "Full HTTP(S) URL to fetch.";
          required = true;
          enum = null;
        };
        grants = {
          packages = [ pkgs.curl ];
          # Security note: wildcard opens any HTTP(S) host through the proxy.
          network.allowedHosts = [ "*" ];
          writable = [ ];
          unrestricted = false;
        };
        runner = "curl -fsSL {url}";
      }

      # Plain TCP clients do not obey the HTTP proxy. Keep examples like this
      # denied until a network namespace/firewall supervisor exists.
      {
        name = "run_migration";
        description = "Disabled until plain TCP database egress has namespace-level routing.";
        policy = "deny";
        params = {
          direction = {
            type = "string";
            description = "Migration direction.";
            required = true;
            enum = [
              "up"
              "down"
            ];
          };
          steps = {
            type = "integer";
            description = "Number of migration steps.";
            required = false;
            enum = null;
          };
        };
        grants = {
          packages = [ pkgs.postgresql ];
          network.allowedHosts = [ "localhost:5432" ];
          writable = [ "migrations" ];
          unrestricted = false;
        };
        runner = "psql -h localhost -p 5432 -f migrations/{direction}/latest.sql";
      }

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
