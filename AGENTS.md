# AGENTS.md

Tartarus: a Nix-defined containment runtime for auditable agents. Agents are
declared in a Nix flake; Nix compiles them to a `manifest.json` bundle; the
Python harness (`tartarus/`) runs the agent loop and jails every tool call in
bubblewrap.

## Architecture (the boundary that matters)

Two languages, one contract between them:

- **Nix side** (`lib/agents.nix`, `agent.nix`, `flake.nix`): compiles an agent's
  capabilities into `.#agents.<system>.<name>.config.build.bundle` — a store path containing
  `manifest.json` plus symlinks to each grant's closure. The manifest
  references every store path it names, so `nix copy <bundle>` pulls the full
  closure.
- **Python side** (`tartarus/`): reads `<bundle>/manifest.json` and runs. The
  harness makes **no `nix` calls at runtime**; the manifest is the only
  bridge. Entry point is `main.py` → `tartarus.cli:main`.
- `PLAN.md` is the authoritative design doc (architecture, manifest contract,
  security invariants, glossary). README is the user-facing quick start. If
  prose and code disagree, code wins.

## Developer commands

```sh
uv run pytest                          # full suite
uv run pytest tests/test_cli.py        # one file
uv run pytest -k "parse_agent"         # one test / pattern
uv run ruff check .                    # lint (passes clean on defaults)
uv run ty check                        # typecheck (Astral ty, NOT mypy; passes clean)
uv run python main.py "prompt"         # run the agent (see below)
nix build .#agents.x86_64-linux.default.config.build.bundle --no-link --print-out-paths
```

`uv` supplies `ruff` and `ty` from the `dev` dependency group; the `nix
develop` shell does **not** provide them. `pyproject.toml` configures only
pytest (`pythonpath=["."]`, `testpaths=["tests"]`); ruff/ty run on defaults.
Python `>=3.13`.

## Running the agent

`uv run python main.py` builds the selected flake agent via `nix` on first run,
so it needs **nix + network + an API key**. Skip the build by pointing at a
prebuilt bundle: `TARTARUS_BUNDLE=/nix/store/...-bundle uv run python main.py`.

Required env: `TARTARUS_API_KEY` (or `OPENCODE_API_KEY`). Defaults target
OpenCode Zen (`https://opencode.ai/zen/v1`, model `glm-5.2`). Per-field
precedence: **explicit env var > agent's `model` block > built-in default**.
API keys and request headers are **env-only** — never put them in the flake.

Session flags: `--continue`, `--resume <id>`, `--no-session`, `--list-sessions`.
Agent selector: `.#<name>` as the first positional arg, or `TARTARUS_AGENT`.

## Test prerequisites and quirks

- `tests/test_jail.py` integration tests require **Linux + `bwrap` + `nix`**
  and `@pytest.mark.skipif`-skip otherwise. They also call
  `tartarus.shell.resolve_minimal_shell_path`, which runs
  `nix build nixpkgs#coreutils nixpkgs#bash` — so they need **nix network
  access**, not just the `bwrap` binary. Without it these tests skip or fail.
- No `conftest.py`. Shared manifest fixtures live in
  `tests/manifest_fixtures.py` — import it directly.
- Pure-Python tests (e.g. `test_cli.py`, `test_config.py`) run hermetically and
  fast; use them for tight loops.

## Sandbox / security invariants (do not break these)

From `tartarus/jail.py` and `PLAN.md §8`:

- Every brokered tool call runs under `bwrap --unshare-all` with only the
  declared closure's store paths bound **read-only** — never the whole
  `/nix/store`. A capability reaches only its declared closure.
- Work tree is mounted at `/work`, read-only unless a `writable` grant re-binds
  a path. Writable paths must be relative and stay under the work tree.
- Package grants append to PATH for **that one call only**; they are not
  permanent. Network grants route through a filtering HTTP proxy with an
  allow-list of `host:port`. **Raw TCP egress is not contained** (no network
  namespace firewall) — that's why plain-TCP capabilities like `run_migration`
  ship as `policy = "deny"`.
- `unrestricted = true` grants **skip bwrap entirely** after policy approval
  (the "big red button"); the manifest validator rejects `unrestricted + auto`.
- Policies: `auto`, `ask-once`, `ask-always`, `deny`. `deny` capabilities are
  never exposed as tools. `TARTARUS_HEADLESS=1` makes `ask-*` fail closed.

## Editing the agent and capabilities (`agent.nix`, `agent-modules/`, `lib/agents.nix`)

- Agents are NixOS-style module graphs passed to `tartarus.lib.tartarusAgent`,
  which takes `{ system, modules, specialArgs }` like `nixpkgs.lib.nixosSystem`.
  Reusable entries under `tartarus.modules` (defined in `agent-modules/default.nix`,
  exposed as the flake's `modules` output) are ordinary agent modules: they can
  set capabilities, prompts, shell packages, imports, or any other agent option.
  `tartarus.modules.coding` imports the common coding set; `tartarus.modules.default`
  aliases it.
- The package set comes from a NixOS-style `nixpkgs` module: `nixpkgs.hostPlatform`
  defaults to `system`; set `nixpkgs.config`/`nixpkgs.overlays`/`nixpkgs.pkgs` in a
  module to override it. Modules receive the result as `pkgs` — there is no `pkgs`
  function argument.
- The `name` option labels the bundle derivation (`tartarus-<name>-bundle`),
  mirroring `networking.hostName`. It defaults to `agent`; set it per agent
  (conventionally matching the attr key) — the attr key is not auto-inherited.
- `tartarusAgent` returns the `evalModules` result (`config`/`options`/`pkgs`/
  `extendModules`), like `nixosSystem`. Build outputs are at
  `config.build.{manifest,bundle,shell}`; assertions are checked when a build
  output is forced (reading `config` is free), mirroring `system.build.toplevel`.
- Capabilities are keyed attrsets under `capabilities.<name>`. Do not put
  `name` in the capability body; the attrset key is the identity.
- The agent's `shell` is the baseline PATH baked into the manifest; keep it
  minimal. Tool-specific programs go in that capability's `grants.packages`.
- `kind = "background"` launches detached (handle returned immediately);
  `kind = "control"` capabilities (`bg_status`/`bg_output`/`bg_stop`) carry no
  runner and no grants — they act on the background registry, not the jail.

## Repo conventions

- `.tartarus/` (audit logs, sessions, background logs) and
  `result`/`result-*` (Nix build outputs) are gitignored runtime/build state.
- Commit style: lowercase conventional-commit prefixes with a scope
  (`tests:`, `jail:`, `agent:`, `manifest_loader:`, `nix/agent:`), short
  subject, no brackets.
- The shipped `default` agent's tools come from two places. The reusable
  `tartarus.modules` (in `agent-modules/default.nix`) supply `read`, `list`,
  `glob`, `grep` (auto), `write`/`edit` (ask-once), `bash` (ask-always), and
  `web_fetch` (ask-always, proxy to any host); `tartarus.modules.coding` bundles
  them and `agent.nix` imports it. `agent.nix` itself adds `jq`,
  `git_status`/`git_diff`/`git_log`/`git_show` (auto), `background_bash`
  (ask-always) with its `bg_status`/`bg_output` (auto) and `bg_stop` (ask-once)
  controls, `format_nix` (nixfmt, ask-once), `pytest` (ask-once, 300s timeout),
  `write_artifact` (ask-always), the networked `pypi_versions` (ask-once) and
  `fetch_rfc` (auto) examples, and a denied `shell_escape` (unrestricted).
