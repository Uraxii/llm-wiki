# Claude Code plugin contract (on-disk shape)

Scope: what a repo must contain so a skill (plus optional commands, agents,
hooks, MCP servers) ships as an installable Claude Code plugin. Every claim
below is from primary Claude Code documentation, cited inline. Corroborated
against the local CLI, `claude 2.1.251`.

Nothing here covers Codex or Copilot. Those harnesses have separate formats and
were not researched under this brief.

Primary sources:

- Create plugins: https://code.claude.com/docs/en/plugins.md
- Plugins reference: https://code.claude.com/docs/en/plugins-reference.md
- Plugin marketplaces: https://code.claude.com/docs/en/plugin-marketplaces.md
- Discover and install plugins: https://code.claude.com/docs/en/discover-plugins.md
- Skills: https://code.claude.com/docs/en/skills.md
- Plugin dependencies: https://code.claude.com/docs/en/plugin-dependencies.md

---

## 1. Manifest: filename, directory, schema

Path is `<plugin-root>/.claude-plugin/plugin.json`. The manifest directory is
`.claude-plugin/` and **only** `plugin.json` goes inside it.
(https://code.claude.com/docs/en/plugins.md, "Create the plugin manifest";
https://code.claude.com/docs/en/plugins-reference.md, "File Locations Reference")

The manifest is itself optional when components sit in default locations, but
without one there is no `name`, so no stable namespace. Reference: ".claude-plugin/
plugin.json | Plugin metadata and configuration (optional)"
(https://code.claude.com/docs/en/plugins-reference.md)

### Required

Exactly one field is required when a manifest is present:

| Field | Type | Rule |
|---|---|---|
| `name` | string | Unique identifier in kebab-case. No spaces, control characters, or bidi-formatting characters. Namespaces every skill the plugin ships (`/name:skill`). |

Source: https://code.claude.com/docs/en/plugins-reference.md, "Required Fields":
"If you include a manifest, only `name` is required."

### Complete schema, verbatim from the reference

```json
{
  "name": "plugin-name",
  "displayName": "Plugin Name",
  "version": "1.2.0",
  "description": "Brief plugin description",
  "author": {
    "name": "Author Name",
    "email": "author@example.com",
    "url": "https://github.com/author"
  },
  "homepage": "https://docs.example.com/plugin",
  "repository": "https://github.com/author/plugin",
  "license": "MIT",
  "keywords": ["keyword1", "keyword2"],
  "metadata": { "catalogId": "cat-123", "tier": "pro" },
  "skills": "./custom/skills/",
  "commands": ["./custom/commands/special.md"],
  "agents": ["./custom/agents/reviewer.md"],
  "workflows": ["./custom/workflows/"],
  "hooks": "./config/hooks.json",
  "mcpServers": "./mcp-config.json",
  "outputStyles": "./styles/",
  "lspServers": "./.lsp.json",
  "experimental": {
    "themes": "./themes/",
    "monitors": "./monitors.json"
  },
  "userConfig": {},
  "channels": [],
  "dependencies": [
    "helper-lib",
    { "name": "secrets-vault", "version": "~2.1.0" }
  ],
  "defaultEnabled": true
}
```

Source: https://code.claude.com/docs/en/plugins-reference.md, "Complete Schema".

Field semantics that matter here
(https://code.claude.com/docs/en/plugins-reference.md, "Metadata Fields" and
"Component Path Fields"):

- `$schema` string, `"https://json.schemastore.org/claude-code-plugin-manifest.json"`.
  Editor autocomplete only; Claude Code ignores it at load.
- `displayName` string, picker label, falls back to `name`, not used for lookup.
- `version` string, semver, optional. Setting it pins the plugin to that string.
- `description` string, shown in the plugin manager.
- `author` object, `{name, email?, url?}`.
- `homepage`, `repository`, `license`, `keywords` (array) all optional metadata.
- `metadata` object, free-form, Claude Code never reads it.
- `defaultEnabled` boolean, default `true`, user settings win.
- `dependencies` array of plugin names or `{name, version}` semver constraints.
  These are OTHER PLUGINS, not system packages. See section 5.

Component path field rules
(https://code.claude.com/docs/en/plugins-reference.md, "Path Behavior Rules"):

- Replaces the default: `commands`, `agents`, `workflows`, `outputStyles`,
  `experimental.themes`, `experimental.monitors`.
- Adds to the default: `skills` (the default `skills/` scan always happens).
- Own merge rules: `hooks`, `mcpServers`, `lspServers`.
- All paths are plugin-root-relative and must start with `./`. Exception: the
  `skills` field also accepts `"."`. Both `"."` and `"./"` mean the plugin root.
  Before v2.1.221 `"."` failed validation, so use `"./"` for older-version support.

---

## 2. Where skills live, and what names them

Default location: `<plugin-root>/skills/<name>/SKILL.md`.
(https://code.claude.com/docs/en/plugins-reference.md, "File Locations Reference";
https://code.claude.com/docs/en/plugins.md, "Add a skill")

Single-skill shortcut: "A plugin that ships exactly one skill can place
`SKILL.md` directly at the plugin root instead of creating a `skills/`
directory." (https://code.claude.com/docs/en/plugins.md, "Plugin structure overview")

Name derivation, from https://code.claude.com/docs/en/skills.md ("How a skill
gets its command name"):

| Layout | Name comes from | Example |
|---|---|---|
| Plugin `skills/` subdirectory | Frontmatter `name` **or** the directory name, namespaced by plugin | `my-plugin/skills/review/SKILL.md` -> `/my-plugin:review`; with `name: fancy` -> `/my-plugin:fancy` |
| Plugin root `SKILL.md` | Frontmatter `name`, plugin directory name as fallback | `my-plugin/SKILL.md` with `name: review` -> `/my-plugin:review` |

So for a plugin skill, frontmatter `name` DOES drive the invocation name, unlike
a personal or project skill where `name` is only a display label and the
directory name wins. Verbatim: "In a personal or project skill, `name` sets only
the display label shown in skill listings, and the command still comes from the
directory name. In a plugin skill, `name` sets the last segment of the command
and the plugin prefix stays in place." (https://code.claude.com/docs/en/skills.md)

Prefix doubling: if `name` already starts with the plugin prefix
(`name: my-plugin:fancy`), v2.1.246+ does not add it again. v2.1.216 through
v2.1.245 doubled it. (https://code.claude.com/docs/en/skills.md)

Allowed SKILL.md frontmatter keys, as reported by the validator error quoted in
the docs: `allowed-tools, compatibility, description, license, metadata, name`.
(https://code.claude.com/docs/en/skills.md) `description` is the field Claude
reads to decide when to auto-load the skill.
(https://code.claude.com/docs/en/plugins.md, "Add Skills to your plugin")

Plugin skills are always namespaced `/plugin-name:skill-name`, so they never
collide with a same-named personal or project skill; both remain available.
(https://code.claude.com/docs/en/plugins.md, "Why namespacing?" and
"What changes when migrating")

---

## 3. Where the other components live

All at the PLUGIN ROOT, never inside `.claude-plugin/`. The docs call this out
as the common mistake: "Don't put `commands/`, `agents/`, `skills/`, or `hooks/`
inside the `.claude-plugin/` directory. Only `plugin.json` goes inside
`.claude-plugin/`." (https://code.claude.com/docs/en/plugins.md, "Plugin
structure overview")

| Component | Default location | Note |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | optional |
| Skills | `skills/<name>/SKILL.md` | |
| Commands | `commands/*.md` | flat Markdown skills; docs say use `skills/` for new plugins |
| Agents | `agents/*.md` | subagent definitions |
| Workflows | `workflows/` | workflow script files |
| Hooks | `hooks/hooks.json` | same `hooks` object shape as `settings.json` |
| MCP servers | `.mcp.json` | at plugin root |
| LSP servers | `.lsp.json` | at plugin root |
| Monitors | `monitors/monitors.json` | background monitors |
| Output styles | `output-styles/` | |
| Themes | `themes/` | |
| Executables | `bin/` | added to the Bash tool `PATH` while the plugin is enabled |
| Settings | `settings.json` | plugin root; only `agent` and `subagentStatusLine` keys supported |

Source: https://code.claude.com/docs/en/plugins-reference.md, "File Locations
Reference" and "Plugin Directory Structure";
https://code.claude.com/docs/en/plugins.md, "Plugin structure overview" and
"Ship default settings with your plugin".

Hooks file shape, verbatim from https://code.claude.com/docs/en/plugins.md
("Migrate hooks"). The `hooks` object is copied unchanged from settings.json:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{ "type": "command", "command": "jq -r '.tool_input.file_path' | xargs npm run lint:fix" }]
      }
    ]
  }
}
```

`bin/` caveat: a plugin distributed through claude.ai organization settings
cannot include a top-level `bin/` directory.
(https://code.claude.com/docs/en/plugins-reference.md, "bin/ Directory and PATH";
https://code.claude.com/docs/en/plugins.md, directory table)

---

## 4. Installing a plugin

### 4a. Local path, no marketplace, development

```bash
claude --plugin-dir ./my-plugin
```

Loads the plugin for that session only. Accepts a `.zip` of the plugin directory,
and can be repeated for multiple plugins. A `--plugin-dir` plugin outranks an
installed marketplace plugin of the same name for that session.
(https://code.claude.com/docs/en/plugins.md, "Test your plugins locally")

In-session, `/reload-plugins` picks up edits without a restart.
(https://code.claude.com/docs/en/plugins.md)

### 4b. Skills-directory plugin, no marketplace, no install step

"Any folder under a skills directory that contains a `.claude-plugin/plugin.json`
manifest is loaded as a plugin named `<name>@skills-dir` on the next session,
with no marketplace and no install step."
(https://code.claude.com/docs/en/plugins-reference.md, "Skills-Directory Plugins")

- `~/.claude/skills/<name>/` -> personal scope, loads in every project.
- `<project-root>/.claude/skills/<name>/` -> project scope, loads only after the
  trust dialog. Warning from the same section: project-scope `@skills-dir`
  plugins load only from `.claude/skills/` of the session's PRIMARY working
  directory, they do not walk up to the repo root, so launching from a
  subdirectory misses one at the repo root.

Scaffold one with:

```bash
claude plugin init my-tool
```

which creates `~/.claude/skills/my-tool/` with a `.claude-plugin/plugin.json`
and a starter `SKILL.md`. (https://code.claude.com/docs/en/plugins.md, "Develop a
plugin in your skills directory")

### 4c. Marketplace, the shareable path

Marketplace manifest lives at `<repo-root>/.claude-plugin/marketplace.json`.
Required fields, verbatim shape from
https://code.claude.com/docs/en/plugin-marketplaces.md:

```json
{
  "name": "marketplace-identifier",
  "owner": {
    "name": "Owner Name"
  },
  "plugins": [
    {
      "name": "plugin-name",
      "source": "./path/to/plugin"
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Marketplace identifier, kebab-case. This is the `@marketplace-name` users type. |
| `owner` | object | Requires `name`; optional `email`, `url`. |
| `plugins` | array | Entries with at minimum `name` and `source`. |

Optional marketplace-level fields seen in the reference example: `$schema`,
`description`, `version`, `metadata.pluginRoot`,
`allowCrossMarketplaceDependenciesOn`, `renames`.
Optional per-entry fields: `displayName`, `description`, `version`, `author`,
`homepage`, `repository`, `license`, `keywords`, `category`, `tags`, `strict`,
`defaultEnabled`, `skills`, `commands`, `agents`, `hooks`, `mcpServers`,
`headers`, `headersHelper`.
(https://code.claude.com/docs/en/plugin-marketplaces.md, "Optional Plugin Fields",
"Full Marketplace Schema Example")

Entry `source` types, all from the same page:

- Relative path: `"./plugins/my-plugin"`. Resolves from the marketplace root, must
  start with `./`, cannot escape with `../`.
- `{"source": "github", "repo": "owner/repo", "ref": "main", "sha": "<40-char>"}`.
- `{"source": "url", "url": "https://gitlab.com/team/plugin.git", "ref": ..., "sha": ...}`.
- `{"source": "git-subdir", "url": ..., "path": "tools/claude-plugin", "ref": ..., "sha": ...}` for monorepos.
- `{"source": "npm", "package": "@acme/plugin", "version": "2.1.0", "registry": ...}`.
- `{"source": "archive", "url": "https://.../plugin-2.1.0.zip", "sha256": ...}`, under 256 MiB.
- `{"source": "command", "command": "my-tool claude-plugin-path", "timeout": 60, "mode": "copy"}`,
  runs a local tool to produce the plugin directory, re-run once per session.

User commands (https://code.claude.com/docs/en/discover-plugins.md and
https://code.claude.com/docs/en/plugin-marketplaces.md):

```bash
# add the catalog
claude plugin marketplace add owner/repo             # GitHub shorthand
claude plugin marketplace add owner/repo@branch
claude plugin marketplace add https://gitlab.com/team/plugins.git
claude plugin marketplace add https://gitlab.com/team/plugins.git#v1.0.0
claude plugin marketplace add ./my-marketplace       # local directory
claude plugin marketplace add ./path/to/marketplace.json
claude plugin marketplace add https://example.com/marketplace.json

# install
claude plugin install plugin-name@marketplace-name
claude plugin install plugin-name@marketplace-name --yes
claude plugin install formatter@your-org --scope project

# lifecycle
claude plugin list
claude plugin update plugin-name@marketplace-name
claude plugin uninstall plugin-name@marketplace-name
claude plugin validate ./my-plugin
```

Same verbs exist in-session as `/plugin marketplace add ...`,
`/plugin install ...`, `/plugin update ...`, `/plugin disable|enable|uninstall`,
plus `/plugin` for the four-tab manager (Discover, Installed, Marketplaces,
Errors). (https://code.claude.com/docs/en/discover-plugins.md)

Install scopes: user (all projects), project (`.claude/settings.json`, shared
with collaborators), local (this repo, just you), plus admin-set managed scope.
`claude plugin install` defaults to user scope unless `--scope` is passed.
(https://code.claude.com/docs/en/discover-plugins.md, "Install plugins")

Team auto-add without an install command: put the marketplace in the project's
`.claude/settings.json` under `extraKnownMarketplaces`. Verbatim example:

```json
{
  "extraKnownMarketplaces": {
    "my-team-tools": {
      "source": {
        "source": "github",
        "repo": "your-org/claude-plugins"
      }
    }
  }
}
```

Since v2.1.195 this adds the CATALOG but does not auto-install a plugin from an
external source; the user still runs `claude plugin install`.
(https://code.claude.com/docs/en/discover-plugins.md, "Configure team marketplaces")

---

## 5. External binary dependency, a Python console script

**A plugin cannot declare or install a system/PyPI binary dependency.** Stated
plainly because the docs give no mechanism for it.

- `dependencies` in `plugin.json` means OTHER CLAUDE CODE PLUGINS, optionally
  with semver constraints: `["helper-lib", {"name": "secrets-vault", "version": "~2.1.0"}]`.
  "When a plugin requires another that is active, Claude Code enables dependencies
  transitively at the same scope."
  (https://code.claude.com/docs/en/plugins-reference.md, "External Dependencies" /
  "Plugin dependencies")
- For external binaries the docs are explicit that the user installs them:
  "**External binary dependencies**: You must install language server binaries
  separately. LSP plugins configure how Claude Code connects to a language server,
  but they don't include the server itself." Failure mode is
  `Executable not found in $PATH` in the `/plugin` Errors tab.
  (https://code.claude.com/docs/en/plugins-reference.md, "External Dependencies";
  echoed at https://code.claude.com/docs/en/discover-plugins.md: "Install the
  language server binary from the table below before using these plugins; the
  plugin doesn't install it for you.")

Three documented mechanisms that get near the goal without being a package manager:

1. `bin/` at the plugin root. "Executables added to the Bash tool's `PATH` and
   invokable as bare commands while the plugin is enabled."
   (https://code.claude.com/docs/en/plugins-reference.md, "File Locations Reference")
   A wrapper shell script here could exec a bundled or user-installed `llmwiki`.
   Not usable for claude.ai-org-distributed plugins (same page).
2. `${CLAUDE_PLUGIN_DATA}`: "Persistent directory that survives plugin updates,
   created on first reference", explicitly intended for "Installed dependencies
   such as `node_modules` or Python virtual environments, generated code, and
   caches." (https://code.claude.com/docs/en/plugins-reference.md,
   "Environment Variables") A plugin can therefore bootstrap a venv there, but
   the docs describe the directory, not an automatic install step.
3. Marketplace `command` source: `{"source": "command", "command": "my-tool
   claude-plugin-path"}` runs a local tool to produce the plugin directory,
   which presupposes the tool is already installed.
   (https://code.claude.com/docs/en/plugin-marketplaces.md, "Command Source")

AMBIGUOUS: whether a `SessionStart` hook is an acceptable / reliable place to
run `uv tool install .` on first use is not addressed by any doc read here. To
settle it, write such a hook into a scratch plugin, load it with
`claude --plugin-dir`, and check the hook fired and its exit code in the debug
log (`claude --debug`, per
https://code.claude.com/docs/en/plugins.md "Test your plugins locally").

---

## 6. Same repo as the code it wraps

**Yes.** Documented and empirically confirmed.

Docs: "**Plugins can live in the same repository as other code.** Use
subdirectories to organize them", with the layout
(https://code.claude.com/docs/en/plugin-marketplaces.md, "Plugin Organization"):

```
repo/
  .claude-plugin/
    marketplace.json
  plugins/
    code-formatter/
      .claude-plugin/
        plugin.json
      skills/
        format/
          SKILL.md
  src/
    (other project code)
```

Path constraints (https://code.claude.com/docs/en/plugin-marketplaces.md,
"Path Constraints"):

- Relative `source` must start with `./` and resolve inside the marketplace repo.
- No `../` escaping the marketplace.
- With `metadata.pluginRoot` set, bare names like `"formatter"` work in place of
  `"./plugins/formatter"`.
- Within a plugin, skills/commands/agents/hooks paths must stay inside the plugin
  directory.

Repo root as BOTH marketplace root and plugin root: docs do not show this case;
`source: "./"` is not in any example. Verified empirically instead. A directory
holding `.claude-plugin/marketplace.json` + `.claude-plugin/plugin.json` +
`skills/llm-wiki/SKILL.md`, with the entry `{"name": "llm-wiki", "source": "./"}`,
passes validation on claude 2.1.251:

```
$ claude plugin validate <dir>
Validating marketplace manifest: <dir>/.claude-plugin/marketplace.json

⚠ Found 2 warnings:

  ❯ description: No marketplace description provided. ...
  ❯ plugins[0] plugin.json → author: No author information provided. ...

✔ Validation passed with warnings
```

That proves the manifests parse and the entry resolves. It does NOT prove an
end-to-end install from a git remote behaves identically. Safer, fully documented
alternative for agent-kb: keep `.claude-plugin/marketplace.json` at the repo root
and put the plugin in a subdirectory, e.g. `source: "./plugin"`, with
`plugin/skills/llm-wiki/SKILL.md`. `pyproject.toml`, `llmwiki/`, and `tests/`
stay at the repo root untouched, which the "Plugin Organization" section
sanctions directly.

---

## 7. Version and compatibility, and how updates arrive

Version fields:

- `plugin.json` `version`, optional semver string. "Setting this pins the plugin
  to that version string." Users only receive updates when the author bumps it.
  If both `plugin.json` and the marketplace entry set a version, `plugin.json`
  wins. If omitted, the version comes from the marketplace entry or other
  sources. (https://code.claude.com/docs/en/plugins-reference.md, "Version
  Management"; https://code.claude.com/docs/en/plugins.md manifest field table)
- Marketplace entry `version` (optional) and marketplace-level `version`.
  (https://code.claude.com/docs/en/plugin-marketplaces.md)
- `compatibility` is an allowed SKILL.md frontmatter key
  (https://code.claude.com/docs/en/skills.md). Its semantics were not covered in
  the pages read. AMBIGUOUS: fetch
  https://code.claude.com/docs/en/skills.md in full and read the frontmatter
  table row for `compatibility` to settle it.
- `dependencies` semver constraints are per-plugin, e.g. `"~2.1.0"`
  (https://code.claude.com/docs/en/plugins-reference.md).

How updates are pulled (https://code.claude.com/docs/en/discover-plugins.md):

- Manual: `claude plugin update plugin-name@marketplace-name`, restart or
  `/reload-plugins` to apply.
- Marketplace refresh: `claude plugin marketplace update <name>`.
- Auto-update: per-marketplace toggle in `/plugin` -> Marketplaces. Claude Code
  checks after session start with a random delay of up to ten minutes; updated
  plugins prompt for `/reload-plugins` or load next launch. Anthropic official
  marketplaces default ON; "Third-party and local development marketplaces have
  auto-update disabled by default."
- Named installs (`plugin@marketplace`) refresh the marketplace before lookup as
  of v2.1.232, even with auto-update off.
- `DISABLE_AUTOUPDATER` turns off plugin auto-updates too;
  `FORCE_AUTOUPDATE_PLUGINS=1` keeps plugin updates while disabling the CLI's.
- `command`-source plugins re-resolve once per session on their own cadence.
- Git-source entries can pin with `ref` (branch/tag) or a 40-char `sha`
  (https://code.claude.com/docs/en/plugin-marketplaces.md).
- `claude plugin tag [path]` creates a `{name}--v{version}` git tag for a release
  and validates that `plugin.json` and the enclosing marketplace entry agree
  (local `claude plugin --help`, v2.1.251; not found in the doc pages read).

---

## 8. Plugin-relative path variables

Three exist (https://code.claude.com/docs/en/plugins-reference.md,
"Environment Variables"):

| Variable | Resolves to | Use for |
|---|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | Absolute path to the plugin's installation directory | Scripts, binaries, config files bundled with the plugin |
| `${CLAUDE_PLUGIN_DATA}` | Persistent directory that survives plugin updates, created on first reference | Installed dependencies such as `node_modules` or Python virtualenvs, generated code, caches |
| `${CLAUDE_PROJECT_DIR}` | The project root | Project-local scripts and config |

Where the placeholders resolve (same section):

| Component | Fields |
|---|---|
| Skill and agent content | anywhere the placeholder appears |
| Hook and monitor commands | anywhere the placeholder appears |
| MCP `stdio` servers | `command`, `args`, `env` |
| MCP `http`/`sse`/`ws` servers | `url`, `headers`, `headersHelper` |
| LSP servers | `command`, `args`, `env`, `workspaceFolder` |

Quoting rule, verbatim: "In hook commands, use exec form with `args` so each path
is passed as one argument. In shell-form hooks and monitor commands, wrap
variables in double quotes":

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}\"/scripts/process.sh"
          }
        ]
      }
    ]
  }
}
```

---

## 9. Local CLI corroboration

`claude --version` -> `2.1.251 (Claude Code)`.

`claude plugin --help`, verbatim:

```
Usage: claude plugin|plugins [options] [command]

Manage Claude Code plugins

Options:
  -h, --help                           Display help for command

Commands:
  details [options] <name>             Show a plugin's component inventory and
                                       projected token cost
  disable [options] [plugin]           Disable an enabled plugin
  enable [options] <plugin>            Enable a disabled plugin
  eval [options] [target]              Run eval cases (<eval dir>/**/case.yaml
                                       or prompt.md + graders/*.md; the eval dir
                                       is evals/ unless --eval-dir or the
                                       manifest says otherwise) against a plugin
                                       and report scored results. Target is a
                                       path, a plugin name, or a
                                       `plugin@marketplace` id — installed and
                                       skills-dir plugins both resolve (and add
                                       a no-plugin baseline arm)
  help [command]                       display help for command
  init|new [options] <name>            Scaffold a new plugin at
                                       ~/.claude/skills/<name>/ (auto-loads next
                                       session as <name>@skills-dir)
  install|i [options] <plugin>         Install a plugin from available
                                       marketplaces (use plugin@marketplace for
                                       specific marketplace)
  list [options]                       List installed plugins
  marketplace                          Manage Claude Code marketplaces
  prune|autoremove [options]           Remove auto-installed dependencies that
                                       are no longer needed
  tag [options] [path]                 Create a {name}--v{version} git tag for a
                                       plugin release, validating that
                                       plugin.json and any enclosing marketplace
                                       entry agree
  uninstall|remove [options] <plugin>  Uninstall an installed plugin
  update [options] <plugin>            Update a plugin to the latest version
                                       (restart required to apply)
  validate [options] <path>            Validate a plugin or marketplace
                                       manifest, or the skills, agents, and
                                       commands in a directory
```

Note `claude plugin details <name>` and `claude plugin prune` do not appear in
the doc pages read; they are corroboration from the installed CLI only.

---

## 10. Open questions

- AMBIGUOUS: `source: "./"` (repo root serving as both marketplace and plugin) is
  undocumented. Validation passes locally. To settle end-to-end: push a branch
  with both manifests, run `claude plugin marketplace add <git url>#<branch>`
  from a scratch HOME, then `claude plugin install llm-wiki@agent-kb`, and check
  the `/plugin` Errors tab.
- AMBIGUOUS: SKILL.md `compatibility` frontmatter semantics. Read the frontmatter
  table on https://code.claude.com/docs/en/skills.md.
- AMBIGUOUS: whether a `SessionStart` hook may install the `llmwiki` console
  script into `${CLAUDE_PLUGIN_DATA}`. Test with `--plugin-dir` plus `--debug`.
- Not researched: Codex and Copilot plugin/extension formats. Different brief.
