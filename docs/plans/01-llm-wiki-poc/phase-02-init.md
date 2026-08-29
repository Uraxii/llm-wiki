[overview](overview.md)

# Phase 2: init

**Goal.** `init` creates a kb whose every file matches the decisions.

**Changes.** `llmwiki/cli.py` (provisional verb table: `init`, `where`),
`tests/test_cli_init.py`. `init` writes: `config.toml` with `[models]` holding
the live `.4` default `summarize = "deepseek/deepseek-v3.2"` and commented
`embed` and `dedup` lines, commented `[endpoint]`, `[identifiers]`, `[jobs]`
examples; `SCHEMA.md` copied from `docs/design/SCHEMA.skeleton.md`;
`SUMMARIZE.md` stub; `log.md` as `# log`; `.gitignore` containing `vectors/`
(the `.9` follow-up); empty `sources/` and `wiki/`. Refuses if `.kb` exists.

**Data structures.** None new; the verb table is `{name: (fn, usage)}`.

**Verification.** Tests: `init` on a tempdir produces the exact file set and
the config parses with the default present; second `init` refuses. Runtime:
`python3 -m llmwiki --kb /tmp/x init && python3 -m llmwiki --kb /tmp/x where`.
