{
  pkgs,
  tartarus,
  ...
}:

{
  imports = [
    tartarus.modules.coding
  ];

  name = "default";

  model = {
    baseUrl = "https://opencode.ai/zen/v1";
    name = "glm-5.2";
    maxTokens = 32768;
    sampling = {
      temperature = 0.6;
    };
  };

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

  capabilities = {
    jq = {
      description = "Run a jq query against a JSON file in the work tree.";
      policy = "auto";
      params = {
        path = {
          type = "string";
          description = "JSON file to query, relative to the work tree.";
          required = true;
        };
        filter = {
          type = "string";
          description = "jq filter expression, such as '.dependencies'.";
          required = true;
        };
      };
      grants.packages = [ pkgs.jq ];
      runner = "jq {filter} {path}";
    };

    git_status = {
      description = "Show concise Git working tree status.";
      policy = "auto";
      grants.packages = [ pkgs.git ];
      runner = "git status --short";
    };

    git_diff = {
      description = "Show unstaged Git diff for the whole work tree or one path.";
      policy = "auto";
      params.path = {
        type = "string";
        description = "Optional path to diff, relative to the work tree.";
      };
      grants.packages = [
        pkgs.bash
        pkgs.git
      ];
      runner = "bash -c 'if [ -n \"$1\" ]; then git diff -- \"$1\"; else git diff; fi' _ {path}";
    };

    git_log = {
      description = "Show recent Git commits, optionally limited to one path.";
      policy = "auto";
      params = {
        limit = {
          type = "integer";
          description = "Maximum number of commits to show. Defaults to 20.";
        };
        path = {
          type = "string";
          description = "Optional path to limit history to, relative to the work tree.";
        };
      };
      grants.packages = [
        pkgs.bash
        pkgs.git
      ];
      runner = "bash -c 'limit=$1; path=$2; if [ -z \"$limit\" ]; then limit=20; fi; if [ -n \"$path\" ]; then git log --oneline -n \"$limit\" -- \"$path\"; else git log --oneline -n \"$limit\"; fi' _ {limit} {path}";
    };

    git_show = {
      description = "Show one Git revision or a file from one revision.";
      policy = "auto";
      params = {
        revision = {
          type = "string";
          description = "Git revision to inspect. Defaults to HEAD.";
        };
        path = {
          type = "string";
          description = "Optional file path to show from the revision.";
        };
      };
      grants.packages = [
        pkgs.bash
        pkgs.git
      ];
      runner = "bash -c 'revision=$1; path=$2; if [ -z \"$revision\" ]; then revision=HEAD; fi; if [ -n \"$path\" ]; then git show --end-of-options \"$revision:$path\"; else git show --stat --patch --end-of-options \"$revision\"; fi' _ {revision} {path}";
    };

    background_bash = {
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
      };
      grants = {
        packages = [ pkgs.bash ];
        writable = [ "." ];
      };
      runner = "bash -c {command}";
    };

    bg_status = {
      description = "Report whether a background task is still running, or its exit code.";
      policy = "auto";
      kind = "control";
      control = "status";
      params.task = {
        type = "string";
        description = "Background task handle, such as bg-1.";
        required = true;
      };
    };

    bg_output = {
      description = "Read the accumulated stdout/stderr of a background task.";
      policy = "auto";
      kind = "control";
      control = "output";
      params = {
        task = {
          type = "string";
          description = "Background task handle, such as bg-1.";
          required = true;
        };
        offset = {
          type = "integer";
          description = "Byte offset to read from. Defaults to 0 (the whole log).";
        };
      };
    };

    bg_stop = {
      description = "Stop a running background task by signalling its process group.";
      policy = "ask-once";
      kind = "control";
      control = "stop";
      params.task = {
        type = "string";
        description = "Background task handle, such as bg-1.";
        required = true;
      };
    };

    format_nix = {
      description = "Format Nix files in the work tree with nixfmt.";
      policy = "ask-once";
      grants = {
        packages = [
          pkgs.bash
          pkgs.findutils
          pkgs.nixfmt
        ];
        writable = [ "." ];
      };
      runner = "bash -c 'find . -name \"*.nix\" -print0 | xargs -0 nixfmt'";
    };

    pytest = {
      description = "Run the project test suite without network access.";
      policy = "ask-once";
      timeout = 300;
      params.filter = {
        type = "string";
        description = "Optional pytest filter expression.";
      };
      grants = {
        packages = [
          pkgs.bash
          pkgs.python3Packages.pytest
        ];
        writable = [ "." ];
      };
      runner = "bash -c 'if [ -n \"$1\" ]; then pytest -k \"$1\"; else pytest; fi' _ {filter}";
    };

    write_artifact = {
      description = "Write a file under the work tree's artifacts directory.";
      policy = "ask-always";
      params = {
        path = {
          type = "string";
          description = "Artifact path under the configured artifacts directory.";
          required = true;
        };
        content = {
          type = "string";
          description = "Complete artifact content.";
          required = true;
        };
      };
      grants = {
        packages = [
          pkgs.bash
          pkgs.coreutils
        ];
        writable = [ "artifacts" ];
      };
      runner = ''
        bash -c 'case "$1" in /*|*..*) echo "artifact path must stay under artifacts" >&2; exit 2;; esac; mkdir -p artifacts "$(dirname "artifacts/$1")"; printf %s "$2" > "artifacts/$1"' _ {path} {content}
      '';
    };

    pypi_versions = {
      description = "Query Python package versions through the scoped HTTP proxy.";
      policy = "ask-once";
      params.package = {
        type = "string";
        description = "Python package name or requirement to inspect.";
        required = true;
      };
      grants = {
        packages = [ pkgs.python3Packages.pip ];
        network.allowedHosts = [ "pypi.org:443" ];
      };
      runner = "pip --no-cache-dir index versions {package}";
    };

    fetch_rfc = {
      description = "Fetch a plain-text RFC from rfc-editor.org through the scoped HTTP proxy.";
      policy = "auto";
      params.number = {
        type = "integer";
        description = "RFC number to fetch.";
        required = true;
      };
      grants = {
        packages = [ pkgs.curl ];
        network.allowedHosts = [ "www.rfc-editor.org:443" ];
      };
      runner = "curl -fsSL https://www.rfc-editor.org/rfc/rfc{number}.txt";
    };

    shell_escape = {
      description = "Disabled unrestricted host escape for trusted overlays only.";
      policy = "deny";
      grants.unrestricted = true;
      runner = "bash";
    };
  };
}
