# Shelf

Shelf trims a coding-agent session when the prompt crosses a token threshold. The turns that leave the window stay verbatim in a local SQLite store. The agent reads them back with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get`. The cut stays put on later requests, so the prompt cache keeps hitting.

The trimmer is the OpenMem context proxy and its OpenCode port, extracted into this repository. The stored text still uses the OpenMem wire format (`[openmem: …]`, `<openmem-recall>`). Memory recall on a trim is off unless you set `CONTEXT_COMPACTION_RECALL=1`.

You need Node 22.16 or newer (for `node:sqlite`) and, for agents that trim through the proxy, the LiteLLM dev build `v1.109.0-dev.1` on `PATH` as `litellm`. The plugin has no API key of its own. Your agent keeps its own login.

## Install

Clone this repository and leave the clone where it is. Claude Code, Codex, the Copilot CLI, and OpenCode load the plugin from that path.

### Claude Code

```
git clone https://github.com/bcmyguest/shelf.git
claude plugin marketplace add "$PWD/shelf"
claude plugin install shelf@shelf
./plugin/scripts/shelf enable claude
./plugin/scripts/shelf proxy
```

Start a new `claude` session after `enable`. The marketplace install registers the search tools and the compact command. `enable` points `ANTHROPIC_BASE_URL` at `http://127.0.0.1:4101` and stores a LiteLLM key in `~/.config/shelf/proxy.env` (mode 600). A previous base URL is written to `~/.config/shelf/proxy-backup.json` and restored by `shelf disable claude`. `proxy` must stay running.

### Codex

```
git clone https://github.com/bcmyguest/shelf.git
codex plugin marketplace add "$PWD/shelf"
codex plugin add shelf@shelf
./plugin/scripts/shelf enable codex
./plugin/scripts/shelf proxy
```

`enable` sets `model_provider = "shelf"` in `~/.codex/config.toml`. Codex keeps its ChatGPT login: the provider uses `requires_openai_auth` and LiteLLM's `/backend-api/codex` pass-through. Trust the hooks, quit Codex, and start it again.

### Copilot CLI

```
git clone https://github.com/bcmyguest/shelf.git
copilot plugin marketplace add "$PWD/shelf"
copilot plugin install shelf@shelf
```

The plugin registers the search tools and the compact hook. Copilot's model URL is not rewritten. Trimming on Copilot requires the CLI to send model traffic through the proxy yourself.

### Pi

```
pi install git:github.com/bcmyguest/shelf
```

`/shelf status` and `/shelf compact` call the same command as the other agents. Pi does not rewrite the outgoing model request, so a trim happens only when that request already goes through the proxy.

### Cursor Agent

```
python scripts/build_plugin.py --out ~/.cursor/plugins/local/
```

Reload Cursor and confirm `shelf` appears under local plugins. Cursor's hooks record a compact request and the MCP server serves search. Cursor does not rewrite the model request from a hook.

### OpenCode

OpenCode 2 trims in the plugin. It does not use the proxy.

```
git clone https://github.com/bcmyguest/shelf.git
mkdir -p ~/.config/opencode/plugins
ln -s "$PWD/shelf/plugin/opencode/shelf.js" ~/.config/opencode/plugins/
```

OpenCode 1 is the proxy path: `./plugin/scripts/shelf enable opencode`, then `./plugin/scripts/shelf proxy`. `enable opencode` refuses to set a base URL when `opencode --version` reports major 2.

## Commands

| Command | Effect |
| --- | --- |
| `/shelf:status` | Trims, estimated tokens saved, and fail-opens in the last day |
| `/shelf:compact` | Trim on the next request. The hook writes `sessions/<id>.force` |
| `shelf enable <claude\|codex\|opencode>` | Point that agent at the proxy |
| `shelf disable <claude\|codex\|opencode>` | Remove that pointing and restore the backup |
| `shelf proxy` | Run LiteLLM on `127.0.0.1:4101` with `proxy/config.yaml` |
| `shelf status` | Same numbers as the slash command |

A fail-open sends the request on untrimmed and records the error. The request is not blocked.

## Where state lives

`OPENMEM_STATE_DIR` defaults to `~/.local/share/shelf` for `shelf` and the plugin. The store is `$OPENMEM_STATE_DIR/context`. Schema and file layout: [docs/context-store.md](docs/context-store.md).

| Variable | Default | Effect |
| --- | --- | --- |
| `CONTEXT_PROXY_ENABLED` | `1` | `0` forwards every request unchanged |
| `CONTEXT_TRIM_AT_TOKENS` | unset: 95000 for Haiku, 125000 otherwise | Tokens that trigger a trim |
| `CONTEXT_TRIM_TO_FRACTION` | `0.3` | In-turn tool steps kept while the request stays under this fraction of the threshold |
| `CONTEXT_COMPRESS_OVER_TOKENS` | `4000` | Tool results above this are stored in full and replaced by a short header |
| `CONTEXT_COMPACTION_RECALL` | `0` in `shelf` | `1` asks a memory bank named on the session registration |
| `CONTEXT_PROXY_PORT` | `4101` | Proxy port |

## Tests

```
python3 -m unittest discover -s proxy/tests
node --test tests/plugin.test.mjs
```

The Python package is still `openmem_context`. `plugin/lib/context-trim.mjs` is the OpenCode port of that package; the two stay on the same store contract.

## License

Apache-2.0. The trimmer comes from [openmem](https://github.com/bcmyguest/openmem).
