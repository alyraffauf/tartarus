# Tartarus

An experimental framework for building composable, hermetic, and shareable AI agents with Nix.

Tartarus turns an agent definition into a runnable, sandboxed system. You
declare the agent's tools, permissions, and policies in a Nix flake. Nix
builds that into a self-contained bundle, and Tartarus handles the agent
loop so that every tool call executes inside a sandbox with only the
access declared for that tool.

- Define an agent's shell, tools, model, and permissions in one Nix flake.
- Compose tools from ordinary nixpkgs packages, granting each tool only
  the binaries it needs so the agent never touches your host machine.
- Share the agent as one Nix closure and run it the same way anywhere.

## Quick Start

Prerequisites:

- Nix with flakes enabled
- Python 3.13+ and `uv`
- Linux with `bubblewrap` available for jailed execution
- An OpenAI-compatible chat-completions endpoint

Set an API key and ask the default agent a question:

```sh
export OPENCODE_API_KEY=...
uv run python main.py "summarize the uncommitted changes in this repo"
```

With no prompt argument, Tartarus starts an interactive REPL:

```sh
uv run python main.py
```

Assistant text, tool starts/finishes, and foreground command output stream live.
Ctrl-C cancels the in-flight turn, tears down any running jailed process, and
returns to the prompt without corrupting the transcript.

## What You Get

- A realized agent bundle at `agents.<system>.<agent>.bundle` containing
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

## Running Agents

By default Tartarus builds and loads:

```text
path:.#agents.<host-system>.default.bundle
```

Select another agent from the same flake with either an env var or an inline
selector:

```sh
export TARTARUS_AGENT=research
uv run python main.py "inspect this project"

uv run python main.py .#default "what packages are available on PyPI for typer?"
```

Point Tartarus at another flake:

```sh
export TARTARUS_FLAKE_REF=github:your-org/your-agents
export TARTARUS_AGENT=default
uv run python main.py
```

Use a prebuilt/copied bundle without needing the source flake at runtime:

```sh
nix build .#agents.x86_64-linux.default.bundle --no-link --print-out-paths
nix copy --to <store-or-cache> /nix/store/...-bundle

# On the receiving machine:
nix copy --from <store-or-cache> /nix/store/...-bundle
export TARTARUS_BUNDLE=/nix/store/...-bundle
uv run python main.py
```

Secrets are never part of the bundle. API keys and deployment-specific headers
come from the environment.

## Defining An Agent

Agents are ordinary Nix values. The reusable compiler lives in `lib/agents.nix`;
the shared coding-agent tool catalog lives in `agentModules`, and the example
agent is in `agent.nix`.

```nix
{
  inputs.tartarus.url = "github:your-org/tartarus";
  inputs.nixpkgs.follows = "tartarus/nixpkgs";

  outputs = { tartarus, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      read_package_json = { pkgs, ... }: {
        name = "read_package_json";
        description = "Read package.json from the work tree.";
        policy = "auto";
        params = { };
        grants.packages = [ pkgs.jq ];
        grants.network.allowedHosts = [ ];
        grants.writable = [ ];
        runner = "jq . package.json";
      };
    in
    {
      agents.${system} = tartarus.lib.mkAgents { inherit pkgs; } {
        default = {
          systemPrompt = "You are a careful coding agent.";
          shell = with pkgs; [ bash coreutils ];
          capabilities = with tartarus.agentModules; [
            read
            write
            edit
            list
            glob
            grep
            bash
            web_fetch
            read_package_json
          ];

          model = {
            provider = "openai-compat";
            baseUrl = "https://opencode.ai/zen/v1";
            name = "glm-5.2";
            maxTokens = 32768;
            sampling = { temperature = 0.6; };
          };
        };
      };
    };
}
```

A capability declares:

- `name`, `description`, and model-facing `params`
- `policy`: `auto`, `ask-once`, `ask-always`, or `deny`
- `grants.packages`: package binaries available only to that tool
- `grants.network.allowedHosts`: proxy-allowed HTTP(S) hosts
- `grants.writable`: work-tree-relative write scopes
- `runner`: the command template
- optional `timeout`, `kind`, and `control` fields for long-running/background
  capabilities

The baseline `shell` is shared by every jailed call, so keep it small. Put
tool-specific programs in that capability's package grants.

`tartarus.agentModules` provides reusable module definitions for common
coding-agent tools: `read`, `list`, `write`, `edit`, `glob`, `grep`, `bash`,
and `web_fetch`. These are also the tool names exposed to the agent.
Task/subagent orchestration, todo state, human questions, and skill loading are
intentionally not modeled as shell capabilities yet.

## Configuration

Backend settings can come from the environment or from the agent's `model` block.
Precedence is per field:

```text
explicit env var > agent model field > built-in default
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
| `TARTARUS_OUTPUT_TRUNCATE` | `10000` | Tool output truncation limit in characters |

To use a local OpenAI-compatible server:

```sh
export TARTARUS_API_KEY=not-used
export TARTARUS_BASE_URL=http://localhost:11434/v1
export TARTARUS_MODEL=llama3.1
uv run python main.py
```

## Sessions And Audit Logs

Every normal run persists its transcript and prints a session id. Resume or
inspect sessions with:

```sh
uv run python main.py "remember the number 42"
uv run python main.py --continue "what number?"
uv run python main.py --resume 20260627-1430 "continue here"
uv run python main.py --list-sessions
uv run python main.py --no-session "one-off"
```

Every brokered tool call appends one JSONL audit record, including policy
decision, grant delta, command, exit code, output length, and errors.

## Repository Map

| Path | Purpose |
|---|---|
| `agent.nix` | Example agent and capabilities |
| `lib/agents.nix` | Nix compiler for `agents.<system>.<agent>.bundle` |
| `tartarus/` | Python harness: config, bundle loading, provider, loop, broker, jail |
| `tests/` | Unit and integration tests |
| `PLAN.md` | Architecture, contract details, and implementation history |

## Development

Run the test suite:

```sh
uv run pytest
```
