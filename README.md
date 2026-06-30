# Tartarus

An experimental framework for building composable, hermetic, and shareable AI agents in Nix.

Define an agent's tools, model, permissions, and prompt the same way you configure NixOS.
Nix compiles it into a self-contained bundle, which the harness consumes at runtime.
A policy broker runs every tool call in a sandbox, granting only the access you declared.

- Compose tools from nixpkgs packages, granting each tool only the binaries and permissions it needs.
- Run the agent in a sandbox with no access to the host machine except what you explicitly allow.
- Share the agent as a single Nix closure. Copy it once and run it identically anywhere.
- Policies (auto, ask-once, ask-always, deny) control which tools need approval and when.

## What It Looks Like

```nix
capabilities.github_api = {
  description = "Query the GitHub API through the scoped HTTP proxy.";
  policy = "auto";
  grants = {
    packages = [ pkgs.curl ];
    network.allowedHosts = [ "api.github.com:443" ];
  };
  runner = "curl -fsSL -H 'Accept: application/vnd.github+json' https://api.github.com{endpoint}";
};

capabilities.write_file = {
  description = "Create or overwrite a file in the work tree.";
  policy = "ask-once";
  grants = {
    packages = [ pkgs.coreutils ];
    writable = [ "." ];
  };
  runner = "bash -c 'mkdir -p \"$(dirname \"$1\")\"; cat > \"$1\"' _ {path}";
};
```

`curl` is only on PATH inside `github_api`, and it can only reach
`api.github.com:443`. `write_file` can write files but only under the work tree.
Policies control when the model needs your approval: `auto` runs freely,
`ask-once` asks once per session, `ask-always` asks every time, and `deny`
hides the tool from the model.

## What You Get

- A realized agent bundle at `agents.<system>.<agent>.config.build.bundle` containing
  `manifest.json`, baked shell PATH, CA bundle, and every referenced store path.
- A provider-neutral agent loop for OpenAI-compatible backends.
- Tool policies: `auto`, `ask-once`, `ask-always`, and `deny`.
- Sandboxed command execution with closure-scoped `/nix/store` bindings and
  scoped work-tree writes.
- Optional network grants through a filtering HTTP proxy.
- Background tools for long-running tasks, with `bg_status`, `bg_output`, and
  `bg_stop` controls.
- Append-only audit logs and resumable session transcripts under `.tartarus/`.

The shipped `default` agent includes read/search tools, Git inspection tools,
`jq`, scoped file editing, formatting/test commands, sealed shell commands,
background commands, artifact writing, and scoped network examples for PyPI,
RFCs, and approved general web fetches. Formatting (`format_nix`) rewrites
files so it is gated with `ask-once`: approve once per session, then nixfmt
runs freely.

## Quick Start

Prerequisites:

- Nix with flakes enabled
- Linux with `bubblewrap` available for jailed execution (`x86_64-linux` or `aarch64-linux`)
- An OpenAI-compatible chat-completions endpoint

Set an API key and ask the default agent a question:

```sh
export OPENCODE_API_KEY=...
nix run .#tartarus -- "summarize the uncommitted changes in this repo"
```

With no prompt argument, Tartarus starts an interactive REPL:

```sh
nix run .#tartarus
```

Assistant text, tool starts/finishes, and foreground command output stream live.
Ctrl-C cancels the in-flight turn, tears down any running jailed process, and
returns to the prompt without corrupting the transcript.

## Start Your Own Agent

To build your own agent instead of running this repo, scaffold a fresh flake
from the bundled template:

```sh
mkdir my-agent && cd my-agent
nix flake init -t github:alyraffauf/tartarus
```

That writes a minimal `flake.nix` and `agent.nix` (the `coding` module plus a
model block). The template's dev shell ships the packaged harness as the
`tartarus` command, so you do not need a checkout of this repo:

```sh
nix develop
export OPENCODE_API_KEY=...
tartarus "summarize this project"
```

Edit `agent.nix` to add capabilities, swap the model, or import other
`tartarus.modules`, then re-run `tartarus`; the first run rebuilds the bundle.
See [Defining An Agent](#defining-an-agent) for the capability schema.

## Running Agents

Tartarus resolves the agent bundle at startup and caches nothing between runs.
The algorithm is:

1. If `TARTARUS_BUNDLE` is set, use that store path directly and skip Nix.
2. Otherwise build `<TARTARUS_FLAKE_REF>#agents.<host-system>.<agent-name>.config.build.bundle`
   via `nix build --no-link --print-out-paths`.

The three slots are:

- `TARTARUS_FLAKE_REF`: the flake reference. Defaults to `path:.` (the current
  directory). Override with `github:org/repo`, `path:/some/dir`, etc.
- `<host-system>`: derived from the host (the harness calls `nix build` for
  `x86_64-linux` or `aarch64-linux`). It is not configurable.
- `<agent-name>`: precedence: an inline `.#<name>` selector as the first
  positional argument wins over `TARTARUS_AGENT`, which wins over `default`.

The inline selector is a harness CLI convention (parsed after `nix run .#tartarus --`),
not a flake output selector. Use `--` to protect it and the prompt from nix's
argument parser:

```sh
# Env selector
export TARTARUS_AGENT=research
nix run .#tartarus -- "inspect this project"

# Inline selector (wins over the env var)
nix run .#tartarus -- .#default "what packages are available on PyPI for typer?"
```

Point Tartarus at another flake:

```sh
export TARTARUS_FLAKE_REF=github:your-org/your-agents
export TARTARUS_AGENT=default
nix run .#tartarus
```

If multiple agents live under `agents.<system>`, name them in the flake and
pick one with `TARTARUS_AGENT` or `.#<name>`:

```nix
agents.${system} = {
  default = tartarus.lib.tartarusAgent { /* ... */ };
  research  = tartarus.lib.tartarusAgent { /* ... */ };
};
```

Use a prebuilt/copied bundle without needing the source flake at runtime:

```sh
# On the build machine:
nix build .#agents.x86_64-linux.default.config.build.bundle --no-link --print-out-paths
nix copy --to <store-or-cache> /nix/store/...-bundle

# On the receiving machine:
nix copy --from <store-or-cache> /nix/store/...-bundle
TARTARUS_BUNDLE=/nix/store/...-bundle nix run github:alyraffauf/tartarus#tartarus
```

Secrets are never part of the bundle. API keys and deployment-specific headers
come from the environment.

## Defining An Agent

Agents are small Nix module systems. The reusable compiler lives in
`lib/agents.nix`; reusable agent modules live under `tartarus.modules`, and the
example agent is in `agent.nix`.

```nix
{
  inputs.tartarus.url = "github:alyraffauf/tartarus";
  inputs.nixpkgs.follows = "tartarus/nixpkgs";

  outputs = { self, tartarus, nixpkgs, ... }:
    let system = "x86_64-linux"; in {
      agents.${system}.default = tartarus.lib.tartarusAgent {
        inherit system;
        modules = [
          tartarus.modules.coding
          ({ pkgs, ... }: {
            name = "default";
            systemPrompt = "You are a careful coding agent.";

            model = {
              baseUrl = "https://opencode.ai/zen/v1";
              name = "glm-5.2";
              maxTokens = 32768;
              sampling = { temperature = 0.6; };
            };

            context = {
              maxChars = 120000;
              recentTurns = 20;
              autoCompact = false;
            };

            capabilities.read_package_json = {
              description = "Read package.json from the work tree.";
              policy = "auto";
              grants.packages = [ pkgs.jq ];
              runner = "jq . package.json";
            };
          })
        ];
      };
    };
}
```

A capability declares:

- the attrset key as its name, plus `description` and model-facing `params`
  (`context_status` and `context_read` are reserved for internal context tools)
- `policy`: `auto`, `ask-once`, `ask-always`, or `deny`
- `grants.packages`: package binaries available only to that tool
- `grants.network.allowedHosts`: proxy-allowed HTTP(S) hosts
- `grants.writable`: work-tree-relative write scopes
- `runner`: the command template
- optional `timeout`, `kind`, and `control` fields for long-running/background
  capabilities

The baseline `shell` is shared by every jailed call, so keep it small. Put
tool-specific programs in that capability's package grants and avoid duplicating
packages that are already defaults (the base shell includes `bash` and `coreutils`).

`tartarusAgent` takes `{ system, modules, specialArgs }`. `nixpkgs.hostPlatform`
defaults to `system`; a module can override the package set with `nixpkgs.config`,
`nixpkgs.overlays`, or `nixpkgs.pkgs`, and every module then receives the result
as `pkgs`. Build outputs live at `config.build.{manifest,bundle,shell}`, hence
`agents.<system>.<name>.config.build.bundle`.

Set `name` per agent (conventionally matching the `agents.<system>.<name>` key) so
its bundle derivation is labelled `tartarus-<name>-bundle` and multi-agent flakes do
not collide. It defaults to `agent` otherwise.

`tartarus.modules.coding` (aliased by `tartarus.modules.default`) imports the common
coding set: `read`, `list`, `glob`, `grep`, `write`, `edit`, `bash`, and `webFetch`.
Single-capability modules are available if you want to assemble a narrower agent.

The optional `context` block declares the agent's context policy: `maxChars` (soft
ceiling on effective context size), `recentTurns` (recent user turns always kept
verbatim), and `autoCompact` (deterministically compact at a turn boundary once over
`maxChars`; defaults to off so compaction stays an explicit, visible action). Any
field left unset falls back to an env override or the built-in default.

## Configuration

Backend and context settings can come from the environment or from the agent's
`model` / `context` blocks. Precedence is per field:

```text
explicit env var > agent model/context field > built-in default
```

API keys and extra request headers are environment-only.

| Env var | Default | Purpose |
|---|---|---|
| `TARTARUS_API_KEY` / `OPENCODE_API_KEY` | required | Bearer key for the backend |
| `TARTARUS_BASE_URL` | `https://opencode.ai/zen/v1` | OpenAI-compatible base URL |
| `TARTARUS_MODEL` | `glm-5.2` | Model id |
| `TARTARUS_MAX_TOKENS` | `16384` | Max completion tokens |
| `TARTARUS_PROVIDER` | `openai-compat` | Provider adapter |
| `TARTARUS_EXTRA_HEADERS` | `{}` | JSON object merged into provider request headers |
| `TARTARUS_FLAKE_REF` | `path:.` | Flake containing the selected agent |
| `TARTARUS_AGENT` | `default` | Agent name under `agents.<system>` |
| `TARTARUS_BUNDLE` | unset | Realized bundle path; skips flake build when set |
| `TARTARUS_WORK_TREE` | current directory | Work tree mounted into the jail as `/work` |
| `TARTARUS_HEADLESS` | `false` | Make `ask-*` policies fail closed |
| `TARTARUS_AUDIT_PATH` | `<work_tree>/.tartarus/audit.jsonl` | Audit log path |
| `TARTARUS_SESSIONS_DIR` | `<work_tree>/.tartarus/sessions` | Session transcript directory |
| `TARTARUS_CONTEXT_DIR` | `<work_tree>/.tartarus/context` | Per-session context ledger directory |
| `TARTARUS_CONTEXT_MAX_CHARS` | `120000` | Soft ceiling on effective context size, in characters |
| `TARTARUS_CONTEXT_RECENT_TURNS` | `20` | Recent user turns always kept verbatim |
| `TARTARUS_OUTPUT_TRUNCATE` | `10000` | Tool output truncation limit in characters |

To use a local OpenAI-compatible server:

```sh
export TARTARUS_API_KEY=not-used
export TARTARUS_BASE_URL=http://localhost:11434/v1
export TARTARUS_MODEL=llama3.1
nix run .#tartarus
```

## Sessions And Audit Logs

Every normal run persists its transcript and prints a session id. Resume or
inspect sessions with:

```sh
nix run .#tartarus -- "remember the number 42"
nix run .#tartarus -- --continue "what number?"
nix run .#tartarus -- --resume 20260627-1430 "continue here"
nix run .#tartarus -- --list-sessions
nix run .#tartarus -- --no-session "one-off"
```

Every brokered tool call appends one JSONL audit record, including policy
decision, grant delta, command, exit code, output length, and errors.

## Limitations

Tartarus makes tradeoffs that matter to some workflows:

- **Linux only.** The sandbox uses `bubblewrap`, which is Linux-specific. You can
  build agent bundles on macOS but cannot run the jail there.
- **Network sandboxing is proxy-based, not a firewall.** When a tool has network
  grants, the jail shares the host's network namespace and filters traffic through
  an HTTP proxy. Raw TCP connections (non-HTTP protocols, direct socket calls) are
  not contained. Without network grants the jail has no network at all.
- **OpenAI-compatible backends only.** The harness ships a single provider adapter
  for OpenAI-compatible chat completions. Anthropic, Gemini, and other vendor APIs
  are not supported.
- **Tools run one at a time.** Within a turn, tool calls are sequential. The model
  cannot dispatch parallel reads, searches, or background tasks in a single
  response.
- **`unrestricted` skips the sandbox entirely.** An unrestricted grant runs the
  command directly on the host, with host filesystem and environment access. The
  manifest validator rejects `unrestricted + auto`, but an approved unrestricted
  call is a full escape.

## Development

The repo is developed from the `nix develop` shell, which supplies Python and
pytest for hacking on the harness. The packaged `tartarus` binary is exposed by
the `tartarus` flake output, not by this dev shell.
