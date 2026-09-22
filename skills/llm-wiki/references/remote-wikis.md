# Asking other wikis

A kb can name other wikis to ask alongside its own, in its own
`config.toml`:

```toml
[remotes.otherwiki]
url = "https://wiki.example.internal"
token_env = "OTHERWIKI_TOKEN"
mode = "read"
```

`url` must be `https`, with no userinfo, query, or fragment.
`token_env` names an environment variable holding that remote's
credential, read fresh on every call; a remote with no `token_env`
sends no credential. That credential is separate from every provider's
`key_env`, which only ever talks to this kb's own model endpoints.
`mode` is advisory; `read` is the only value accepted today.

```
llmwiki search "query" --remote otherwiki
llmwiki search "query" --remote otherwiki --remote another
llmwiki search "query" --all              # every remote in [remotes]
```

`--remote NAME` repeats to name several remotes. `--all` adds every
remote in `[remotes]`. Either flag also asks the local kb, under the
label `local`, as long as the kb has a `wiki/` directory.

Results come back as one ranked list per wiki, under a `# <name>`
header, and are NEVER merged into one ranking. A similarity score is
comparable only inside one embedding model's distribution, so a score
from one wiki and a score from another were never on the same scale;
comparing them is a mistake this tool refuses to make for you. Under
`--remote` or `--all`, the printed line drops the score and shows a
rank inside that wiki's own list only:
`<rank>\t<name>\t<title>\t<updated>\t<size>`. Plain `search`, with no
remote flag, is unchanged and still prints
`<score>\t<name>\t<title>\t<updated>\t<size>`.

A wiki that fails, local or remote, prints `<name>\t<code>` to stderr
instead of a block, and the whole command exits 1, even though every
other wiki's ranking printed fine. Plain `search` keeps its own exit
code and is not affected by this.

`llmwiki page <name>` prints one local page's bytes. `llmwiki page
--remote NAME <page>` fetches that page from a remote instead, and on
failure prints `<name>\t<code>` to stderr and exits 1, the same shape
as a failed search participant.

## The service

`llm-wiki` also ships `llmwiki_service`, the HTTP service that answers
the `/search` and `/page` routes a `[remotes]` entry points at.
Setting one up is an operator task, covered in
`docs/plans/02-llmwiki-service/` and `llmwiki_service/admin.py`, not
here.

One fact worth knowing as a caller: an operator mints your remote
token with `POST /admin/tokens`, which returns the plaintext exactly
once, lists tokens with `GET /admin/tokens`, and revokes one with
`DELETE /admin/tokens/{label}`. The bootstrap admin credential that
mints the first token cannot be revoked at runtime; the operator's
path for that is mint a real token, unset the bootstrap variable, and
restart. If a remote token you were given stops working, that is the
kind of thing to ask the operator about.

The wheel only started shipping `llmwiki_service` recently. An older
install of this package has no service in it at all.
