# The credential

Read from the environment and nowhere else. `config.toml` holds the NAME
of the variable, never a key value, and each provider carries its own:
`key_env` names a variable holding the key, `key_file_env` names one
holding a path to read the key out of. A local endpoint that needs no
key and a hosted one that does can therefore both be named from the same
`[models]` block.

**Nothing sets the variable for you.** A fresh shell has no key, and
every verb that calls a model then stops with

```
llmwiki: embed: LLM_WIKI_API_KEY_HOSTED is unset or empty
```

and exits 1. Fetch the key from wherever this machine keeps its secrets,
this user has a skill for it, and pass it inline to the one command
that needs it. Never write it to a file, never put it in
`config.toml`, never print it, never leave it exported in a shell other
agents share.

A shell may already do this for you: a wrapper that fetches the key per
command and passes it to that one process. If a plain `llmwiki search`
works without you handling a key, that is why, and you should not go
looking for one.

**A wrapper defined as a shell function may not reach you.** A function
lives in the shell that sourced it, so a non-login or snapshotted shell,
which is what most agents run in, can inherit the wrapper's name and
none of the helpers it calls. The symptom is a `command not found` for a
name you never typed. To get past it now, source the file that defines
the function, then rerun the verb.

The durable fix belongs to whoever owns the shell configuration, not to
this CLI. Move the wrapper out of the shell startup file and into an
executable on `PATH`:

```
#!/usr/bin/env bash
# ~/.local/bin/llmwiki-with-key, or any name earlier on PATH
exec env LLM_WIKI_API_KEY_HOSTED="$(your-secret-tool read llm-wiki)" \
  /path/to/real/llmwiki "$@"
```

Every shell inherits a file on `PATH`, login or not, snapshotted or not,
so the failure above cannot happen again. There is no `key_command`
setting in `config.toml`, and there will not be: it would make a config
file executable, which is a much worse trade than one script.
