{ lib }:

let
  empty_grants = {
    network.allowedHosts = [ ];
    writable = [ ];
    unrestricted = false;
  };
in
{
  bash = { pkgs, ... }: {
    name = "bash";
    description = ''
      Run a shell command using only tools available in the shell. The work
      tree is writable and there is no network. Each call requires approval.
    '';
    policy = "ask-always";
    params.command = {
      type = "string";
      description = "The command line to run inside the jail.";
      required = true;
      enum = null;
    };
    grants = empty_grants // {
      packages = [ pkgs.bash ];
      writable = [ "." ];
    };
    runner = "bash -c {command}";
  };

  read = { pkgs, ... }: {
    name = "read";
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
    grants = empty_grants // {
      packages = [
        pkgs.bash
        pkgs.gnused
      ];
    };
    runner = ''
      bash -c 'start=$1; end=$2; if [ -z "$start" ]; then start=1; fi; range="$start,\$"; if [ -n "$end" ]; then range="$start,$end"; fi; sed -n "$range"p "$3"' _ {start_line} {end_line} {path}
    '';
  };

  write = { pkgs, ... }: {
    name = "write";
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
    grants = empty_grants // {
      packages = [
        pkgs.bash
        pkgs.coreutils
      ];
      writable = [ "." ];
    };
    runner = ''
      bash -c 'mkdir -p "$(dirname "$1")"; printf %s "$2" > "$1"' _ {path} {content}
    '';
  };

  edit =
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

        if not replace_all and count != 1:
            lines = [
                text.count("\n", 0, index) + 1
                for index in range(len(text))
                if text.startswith(old_text, index)
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
      name = "edit";
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
      grants = empty_grants // {
        packages = [ edit-file ];
        writable = [ "." ];
      };
      runner = "edit-file {path} {old_str} {new_str} {replace_all}";
    };

  glob =
    { pkgs, ... }:
    let
      glob-files = pkgs.writers.writePython3Bin "glob-files" { flakeIgnore = [ "E501" ]; } ''
        import pathlib
        import sys

        pattern = sys.argv[1]
        root = pathlib.Path(sys.argv[2] or ".")
        for path in sorted(root.glob(pattern)):
            print(path.as_posix())
      '';
    in
    {
      name = "glob";
      description = "Find work-tree paths matching a glob pattern.";
      policy = "auto";
      params = {
        pattern = {
          type = "string";
          description = "Glob pattern to match, such as '**/*.nix'.";
          required = true;
          enum = null;
        };
        path = {
          type = "string";
          description = "Root path to search from, relative to the work tree. Defaults to '.'.";
          required = false;
          enum = null;
        };
      };
      grants = empty_grants // {
        packages = [ glob-files ];
      };
      runner = "glob-files {pattern} {path}";
    };

  list = { pkgs, ... }: {
    name = "list";
    description = "List a directory in the work tree.";
    policy = "auto";
    params.path = {
      type = "string";
      description = "Directory to list, relative to the work tree. Defaults to '.'.";
      required = false;
      enum = null;
    };
    grants = empty_grants // {
      packages = [
        pkgs.bash
        pkgs.coreutils
      ];
    };
    runner = "bash -c 'path=$1; if [ -z \"$path\" ]; then path=.; fi; ls -la \"$path\"' _ {path}";
  };

  grep = { pkgs, ... }: {
    name = "grep";
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
    grants = empty_grants // {
      packages = [
        pkgs.bash
        pkgs.ripgrep
      ];
    };
    runner = ''
      bash -c 'target=$2; if [ -z "$target" ]; then target=.; fi; if [ -n "$3" ]; then rg --glob "$3" "$1" "$target"; else rg "$1" "$target"; fi' _ {pattern} {path} {glob}
    '';
  };

  web_fetch = { pkgs, ... }: {
    name = "web_fetch";
    description = "Fetch any HTTP(S) URL through the scoped HTTP proxy after per-call approval.";
    policy = "ask-always";
    params.url = {
      type = "string";
      description = "Full HTTP(S) URL to fetch.";
      required = true;
      enum = null;
    };
    grants = empty_grants // {
      packages = [ pkgs.curl ];
      network.allowedHosts = [ "*" ];
    };
    runner = "curl -fsSL {url}";
  };
}
