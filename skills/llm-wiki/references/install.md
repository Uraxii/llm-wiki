# Installing the `llmwiki` CLI

The skill folder carries the CLI's source alongside the instructions for
using it, so copying the skill copies the tool. But no plugin format for
Claude Code, Codex, or Copilot CLI can turn that source into a program on
`PATH`. So the CLI is one manual step, once per machine.

Check first, because it is usually already there:

```
llmwiki --help
```

## From the plugin you already have

Installing the plugin clones the whole project, not just the skill, so
the source is already on disk and there is nothing to fetch. Claude Code
keeps it under `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`
and Codex under `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`.
For this plugin the marketplace and the plugin are both named `llm-wiki`,
so:

```
ls ~/.claude/plugins/cache/llm-wiki/llm-wiki/      # the version directories
uv tool install ~/.claude/plugins/cache/llm-wiki/llm-wiki/0.2.0/skills/llm-wiki
```

For Codex, the same directory sits under `~/.codex/plugins/cache/llm-wiki/llm-wiki/<version>/skills/llm-wiki`.

That directory holds `pyproject.toml` and the `llmwiki` package, which is
all `uv tool install` needs.

Install from the version the harness is loading. An upgrade writes a new
version directory beside the old one and leaves the old one in place, so
installing from the wrong one gets you a CLI older than the skill telling
you how to use it.

## From a clone, if you are not using a plugin

```
git clone https://github.com/Uraxii/llm-wiki
uv tool install ./llm-wiki/skills/llm-wiki
```

## What it needs

Python 3.14, and `sqlite-vec`, the project's one dependency. `sqlite-vec`
is not optional: the vector store is loaded at import time and there is no
degraded mode. `uv tool install` pulls it for you.

## Checking it worked

```
llmwiki --help
```

It prints the verbs: `init`, `where`, `ingest`, `summarize`, `dedup`,
`lint`, `embed`, `status`, `search`.

`llmwiki --version` prints the version and the directory the package ran
from. Reach for it when a change to a checkout does not show up at the
command line: an installed copy on `PATH` does not track a checkout, and
the path is the half of that line that tells them apart.
