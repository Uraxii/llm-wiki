# Extension contracts: OpenAI Codex and GitHub Copilot

Question: can this repo ship as an installable extension carrying (a) agent
instructions/skills and (b) custom commands, the way a Claude Code plugin
does? Claude Code is out of scope here; another note covers it.

Retrieved 2026-08-31. Local corroboration from `codex-cli 0.145.0` and
`GitHub Copilot CLI 1.0.80` on this machine.

Headline: **both harnesses now have a real plugin format, and both read
`SKILL.md`.** Neither needs the hand-copy. Copilot reads `.claude/skills/`
directly; Codex needs the skill under `.agents/skills/` or inside a plugin.

## OpenAI Codex

### Instruction files

Codex reads `AGENTS.md`, merged root-to-leaf: global `~/.codex/AGENTS.md`
first, then the repository root, then each deeper directory down to the
current working directory. "Codex concatenates files from the root down,
joining them with blank lines. Files closer to your current directory
override earlier guidance because they appear later in the combined
prompt." Codex stops adding files once combined size reaches
`project_doc_max_bytes` (32 KiB default).

- https://developers.openai.com/codex/guides/agents-md (retrieved 2026-08-31)
- https://learn.chatgpt.com/docs/config-file/config-reference (retrieved 2026-08-31)

`project_doc_fallback_filenames` (array) names alternative files when
`AGENTS.md` is absent. `model_instructions_file` replaces the built-in
instructions entirely.

Local corroboration: `~/.codex/AGENTS.md` exists on this machine and is the
global instruction file.

### Skills

Codex reads skills from four locations:

| Scope | Path |
|---|---|
| Repository | `$CWD/.agents/skills` and `$REPO_ROOT/.agents/skills` |
| User | `$HOME/.agents/skills` |
| Admin | `/etc/codex/skills` |
| System | bundled with Codex by OpenAI |

`SKILL.md` frontmatter requires `name` and `description`. "The `SKILL.md`
file must include `name` and `description`."

- https://developers.openai.com/codex/skills -> https://learn.chatgpt.com/docs/build-skills (retrieved 2026-08-31)

Note the path is `.agents/skills`, **not** `.codex/skills`. Local machine
has `~/.codex/skills/` present; docs name `$HOME/.agents/skills`. Treat
`~/.codex/skills` as UNCONFIRMED by docs, though the directory exists.

### Custom prompts (slash commands)

Supported but **deprecated**. "Custom prompts are deprecated. Use skills for
reusable instructions that Codex can invoke explicitly or implicitly."

- Location: `$CODEX_HOME/prompts/`, default `~/.codex/prompts/`. Non-Markdown
  files ignored.
- Format: one `.md` file per prompt; filename minus `.md` is the prompt name.
  YAML frontmatter carries description and argument hints.
- Arguments: positional `$1`..`$9` and named uppercase placeholders such as
  `$FILE`, supplied as `KEY=value` pairs.
- Invocation: `/prompts:<name>` in the CLI or IDE extension.
- Local only. "Custom prompts require explicit invocation and live in your
  local Codex home directory (for example, `~/.codex`), so they're not shared
  through your repository."

- https://developers.openai.com/codex/custom-prompts -> https://learn.chatgpt.com/docs/custom-prompts (retrieved 2026-08-31)

Consequence: a custom command cannot be shipped by a repo. Skills are the
supported replacement, and a skill can be shipped.

### Plugin format

**A plugin format exists.** Not "no plugin format".

Manifest: `.codex-plugin/plugin.json` at plugin root.

```
plugin-root/
├── .codex-plugin/plugin.json
├── skills/
├── hooks/
├── .app.json
├── .mcp.json
└── assets/
```

"Every plugin has a manifest at `.codex-plugin/plugin.json`. It can also
include a `skills/` directory, a `hooks/` directory for lifecycle hooks, an
`.app.json` file that maps registered MCP server connections, an `.mcp.json`
file that configures bundled MCP servers".

Required manifest fields: `name` (kebab-case), `version`, `description`.
Common optional: `skills` (`"./skills/"`), `mcpServers` (`"./.mcp.json"`),
`apps` (`"./.app.json"`), `hooks` (`"./hooks/hooks.json"`), plus `author`,
`homepage`, `repository`, `license`, `keywords`, and an `interface` object.

Individual skills live at `skills/<skill-name>/SKILL.md`.

Marketplaces:

- repo-local: `$REPO_ROOT/.agents/plugins/marketplace.json`
- personal: `~/.agents/plugins/marketplace.json`
- git-backed: `codex plugin marketplace add owner/repo`

- https://developers.openai.com/plugins/build/plugins (retrieved 2026-08-31)
- https://learn.chatgpt.com/docs/plugins (retrieved 2026-08-31)

Local corroboration, `codex plugin --help`:

```
Manage Codex plugins

Commands:
  add          Install a plugin from a configured marketplace snapshot
  list         List plugins available from configured marketplace snapshots
  marketplace  Add, list, upgrade, or remove configured plugin marketplaces
  remove       Remove an installed plugin from local config and cache
```

Installed plugins cache to `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`.
Observed on this machine, `openai-curated-remote/deep-research-work/0.1.14/`
containing `skills/deep-research/SKILL.md`. That is the real on-disk shape:
plugin ships a skill directory holding a `SKILL.md`.

Enablement is recorded in `~/.codex/config.toml`:

```toml
[plugins."github@openai-curated-remote"]
enabled = true
```

### MCP discovery

`~/.codex/config.toml`, table `mcp_servers.<id>`. Keys: `command`, `args`,
`cwd`, `env`, `url` (HTTP servers), `enabled`, `enabled_tools`,
`disabled_tools`, `default_tools_approval_mode`, `bearer_token_env_var`,
`http_headers`, `env_http_headers`, `auth`, `oauth_resource`,
`oauth.callback_port`, `oauth.callback_url`, `oauth.client_id`, `scopes`,
`required`, `startup_timeout_sec`, `startup_timeout_ms`, `tool_timeout_sec`,
`tools.<tool>.approval_mode`, `experimental_environment`.

Per project: `.codex/config.toml` in the repo, loaded **only when the project
is marked trusted**; some settings cannot be overridden at project level.
So MCP is both per-user and per-project, with trust gating the project half.

Plugins may override their own MCP servers via `plugins.<plugin>.mcp_servers`.

- https://learn.chatgpt.com/docs/config-file/config-reference (retrieved 2026-08-31)

Local corroboration: this machine's `~/.codex/config.toml` carries
`[projects."<path>"] trust_level = "trusted"` entries, and `codex mcp` has
`list/get/add/remove/login/logout` subcommands.

## GitHub Copilot

### Custom instructions (CLI)

Copilot CLI reads:

| Scope | Paths |
|---|---|
| User | `$HOME/.copilot/copilot-instructions.md`, `$HOME/.copilot/instructions/**/*.instructions.md` |
| Repository | `.github/copilot-instructions.md`, `.github/instructions/**/*.instructions.md` |
| Agent files | `AGENTS.md`, `CLAUDE.md` (also `.claude/CLAUDE.md`), `GEMINI.md` |
| Custom | directories named by `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` |

`COPILOT_HOME` replaces `$HOME/.copilot` for the user-level locations.

- https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions (retrieved 2026-08-31)

Repo-wide and path-specific instructions across all Copilot surfaces:
`.github/copilot-instructions.md`, and `NAME.instructions.md` files "within
or below the `.github/instructions` directory in the repository".

- https://docs.github.com/en/copilot/concepts/response-customization (retrieved 2026-08-31)

Local corroboration: `~/.copilot/copilot-instructions.md` and
`~/.copilot/skills/` exist on this machine. `copilot --help` documents
`--no-custom-instructions  Disable loading of custom instructions from
AGENTS.md and related files`.

### Skills

Copilot CLI discovers skills from:

| Scope | Paths |
|---|---|
| Project | `.github/skills/`, `.agents/skills/`, or `.claude/skills/` |
| Personal | `~/.copilot/skills/` or `~/.agents/skills/` |
| Plugin | installed plugins that bundle skills |
| Custom | directories added with `copilot skill add <directory>` |

`SKILL.md` frontmatter: `name` required, "must be lowercase, using hyphens
for spaces"; `description` required; `license` optional; `allowed-tools`
optional, listing tools such as `shell` that Copilot may use without
confirmation.

- https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills (retrieved 2026-08-31)

**`.claude/skills/` is read directly.** That is the single biggest finding
for this repo: Copilot CLI needs no translation layer at all.

Local corroboration, `copilot skill --help`:

```
Skills are reusable instructions (SKILL.md files) that extend Copilot with
specialized capabilities. They are discovered from several sources:
  Project   .github/skills/, .agents/skills/, or .claude/skills/
  Personal  ~/.copilot/skills/ or ~/.agents/skills/
  Plugin    Installed plugins that bundle skills
  Custom    Directories added with `copilot skill add <directory>`
```

### Custom agents and prompt files

Custom agents: `*.agent.md` files. Project `.github/agents/`, personal
`~/.copilot/agents/`. Home directory wins on a name collision.

Frontmatter fields: `name` (optional display name), `description`
(required), `target` (`vscode` or `github-copilot`), `tools`, `model`,
`disable-model-invocation`, `user-invocable`, `infer` (retired, use the
previous two), `mcp-servers` (GitHub.com only, not used in VS Code),
`metadata` (GitHub.com only).

Invoked with `/agent` in interactive mode, by name in a prompt, inferred
from the description, or `copilot --agent <name> --prompt '...'`.

- https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli (retrieved 2026-08-31)
- https://docs.github.com/en/copilot/reference/custom-agents-configuration (retrieved 2026-08-31)

Prompt files: `*.prompt.md`, Markdown, stored in the workspace. Available in
VS Code, Visual Studio, and JetBrains IDEs only; not on the GitHub website,
not Xcode. The response-customization page does not specify their
frontmatter fields, so those are NOT FOUND from a primary source here.

- https://docs.github.com/en/copilot/concepts/response-customization (retrieved 2026-08-31)

### Plugin format

**A plugin format exists, and it is distinct from a Copilot Extension.**

- **CLI plugin**: a directory with a `plugin.json` manifest at its root,
  installed locally. Ships skills, agents, hooks, MCP servers, LSP servers.
  No GitHub App, no server, no chat participant.
- **Copilot Extension**: a GitHub App that adds a chat participant to
  Copilot Chat, running as a hosted service. Different thing entirely, not
  what this repo wants.

CLI plugin layout:

```
my-plugin/
├── plugin.json           # Required manifest
├── agents/               # *.agent.md custom agents
├── skills/               # <name>/SKILL.md
├── hooks.json            # or hooks/
├── .mcp.json             # or .github/mcp.json
└── lsp.json              # or .github/lsp.json
```

"At minimum, it contains a `plugin.json` manifest file at the root of the
directory." Components: "`*.agent.md` files in `agents/`"; "skills
subdirectories in `skills/`, containing a `SKILL.md` file"; "a `hooks.json`
file in the plugin root, or in `hooks/`"; "a `.mcp.json` file in the plugin
root, or an `mcp.json` file in `.github/`"; "an `lsp.json` file in the
plugin root, or in `.github/`".

Install: `copilot plugin install`, the `/plugin install` slash command, or
declaratively via the `enabledPlugins` field in `~/.copilot/settings.json`
or `.github/copilot/settings.json`. Sources: a marketplace, a repository, a
local path. Default marketplaces `github/copilot-plugins` and
`github/awesome-copilot`.

- https://docs.github.com/copilot/concepts/agents/copilot-cli/about-cli-plugins (retrieved 2026-08-31)

Local corroboration, `copilot plugin --help`:

```
Plugins extend Copilot CLI with additional skills, agents, hooks, MCP servers,
and LSP servers. They can be installed from plugin marketplaces, GitHub
repositories, repository subdirectories, or direct git URLs.

Two marketplaces are included by default:
  copilot-plugins   github/copilot-plugins
  awesome-copilot   github/awesome-copilot
```

`copilot plugins install --skill ./my-skill/SKILL.md` installs a bare skill
without a plugin wrapper; `--scope project` puts it in the project instead of
the user account.

### MCP discovery

`~/.copilot/mcp-config.json` is the user-level MCP config. From
`copilot --help`:

```
--additional-mcp-config <json>  Additional MCP servers configuration as JSON
                                string or file path (prefix with @) (can be
                                used multiple times; augments config from
                                ~/.copilot/mcp-config.json for this session)
```

Plugins bundle their own via `.mcp.json` at plugin root or
`.github/mcp.json`. `copilot mcp` manages servers interactively.

## External CLI binary

Both agents run shell commands, so both can call an installed `llmwiki` on
`PATH` with no special support. That is ordinary tool use, not an extension
feature.

**No manifest field declaring an external binary dependency was found in
either format.** Codex `.codex-plugin/plugin.json` documents `name`,
`version`, `description`, `skills`, `mcpServers`, `apps`, `hooks`, `author`,
`homepage`, `repository`, `license`, `keywords`, `interface`; none installs
or requires a binary. Copilot `plugin.json` documents agents, skills, hooks,
MCP servers, LSP servers; same gap. NOT FOUND, both.

Practical workaround, same for both: the `SKILL.md` states the prerequisite
in prose (`uv tool install .`, or `pipx install`), and the skill's first step
checks `command -v llmwiki` and tells the user how to install it if absent.
A Codex plugin's `hooks/` directory is the only documented place a plugin can
run code at a lifecycle point, so an install-check hook is possible there;
whether a hook can gate on a missing binary is UNCONFIRMED, the hooks
reference was not read.

## Recommended install path, one per harness

### Codex

Publish this repo as a Codex plugin.

1. Add `.codex-plugin/plugin.json` with `name`, `version`, `description`, and
   `"skills": "./skills/"`.
2. Keep the skill at `skills/llm-wiki/SKILL.md` where it already lives. The
   layout already matches; only the manifest is missing.
3. User runs `codex plugin marketplace add <owner>/agent-kb`, then
   `codex plugin add llm-wiki@<marketplace>`.

Fallback with zero packaging: user symlinks `skills/llm-wiki` into
`~/.agents/skills/`. Codex reads it from there with no manifest at all.

Do not build a custom prompt for this. Deprecated, local-only, unshippable.

### Copilot

Two options, ranked.

1. **Zero work today**: the skill already sits at `skills/llm-wiki/SKILL.md`.
   `copilot skill add /path/to/agent-kb/skills/llm-wiki` registers it as a
   custom skill directory. If the repo also carries `.claude/skills/`,
   Copilot reads that path in a project checkout with no action at all.
2. **Shippable**: add `plugin.json` at repo root with the skill under
   `skills/`, then `copilot plugin install <owner>/agent-kb`. Same skill
   directory serves both harnesses' plugin formats.

Do not build a Copilot Extension (GitHub App). Wrong mechanism: it is a
hosted chat participant, not an instruction bundle.

## Convergence note

`skills/<name>/SKILL.md` with `name` + `description` frontmatter is read by
Claude Code, Codex, and Copilot CLI. One directory, three harnesses. The
difference is only the wrapper manifest and where the directory is linked
from:

| Harness | Manifest | Bare-skill path |
|---|---|---|
| Codex | `.codex-plugin/plugin.json` | `~/.agents/skills/`, `$REPO_ROOT/.agents/skills/` |
| Copilot CLI | `plugin.json` (root) | `~/.copilot/skills/`, `~/.agents/skills/`, `.github/skills/`, `.agents/skills/`, `.claude/skills/` |

`~/.agents/skills/` is read by both. A single symlink there covers Codex and
Copilot at once, with no manifest, no marketplace, and no hand-copy.

## Uncertain and missing

- `~/.codex/skills/` exists on this machine but docs name
  `$HOME/.agents/skills`. UNCONFIRMED which one Codex 0.145.0 actually reads,
  or whether both.
- Copilot `*.prompt.md` frontmatter fields: NOT FOUND in the primary docs
  read here.
- Codex hooks schema (`hooks/hooks.json` contents): not read. Only the
  environment variables `PLUGIN_ROOT` and `PLUGIN_DATA` were documented on
  the pages fetched.
- Whether a plugin manifest in either format can declare or install an
  external CLI binary: NOT FOUND.
- Both formats are new and moving. Versions checked: `codex-cli 0.145.0`,
  `GitHub Copilot CLI 1.0.80`, both on 2026-08-31. Codex custom prompts were
  already deprecated in favour of skills by this date, and Codex's
  `developers.openai.com/codex/*` doc URLs now 308-redirect to
  `learn.chatgpt.com/docs/*`, which is itself a sign of recent churn.
