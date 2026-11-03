# Smarter Compact

Smarter Compact trims a coding-agent session when the prompt crosses a token threshold. The turns that leave the window stay verbatim in a local SQLite store. The agent reads them back with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get`. The cut stays put on later requests, so the prompt cache keeps hitting.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="https://bcmyguest.github.io/smarter-compact/media/smart-compaction-dark.gif" />
<img src="https://bcmyguest.github.io/smarter-compact/media/smart-compaction.gif" alt="Smart compaction: older turns move into the session store, the cut turn carries a pointer, smart_compact_search recovers a detail." width="720" />
</picture>
</p>

**Docs:** [bcmyguest.github.io/smarter-compact](https://bcmyguest.github.io/smarter-compact/) ([smart compaction](https://bcmyguest.github.io/smarter-compact/context-management.html), [context proxy & port](https://bcmyguest.github.io/smarter-compact/context-proxy.html), [context store](https://bcmyguest.github.io/smarter-compact/context-store.html)).

The trimmer is the OpenMem context proxy and its OpenCode port, extracted into this repository. The stored text still uses the OpenMem wire format (`[openmem: …]`, `<openmem-recall>`). Memory recall on a trim is off unless you set `CONTEXT_COMPACTION_RECALL=1`.

You need Node 22.16 or newer (for `node:sqlite`) and, for agents that trim through the proxy, the LiteLLM dev build `v1.109.0-dev.1` on `PATH` as `litellm`. The plugin has no API key of its own. Your agent keeps its own login.

## Install

Clone this repository and leave the clone where it is. Claude Code, Codex, the Copilot CLI, and OpenCode load the plugin from that path.

### Claude Code

```
git clone https://github.com/bcmyguest/smarter-compact.git
claude plugin marketplace add "$PWD/smarter-compact"
claude plugin install smarter-compact@smarter-compact
./plugin/scripts/sc enable claude
```

Start a new `claude` session after `enable`. The marketplace install registers the search tools and the compact command. `enable` points `ANTHROPIC_BASE_URL` at `http://127.0.0.1:4101`, starts the LiteLLM proxy in the background when needed, and stores a key in `~/.config/smarter-compact/proxy.env` (mode 600). Session hooks also ensure the proxy is up. A previous base URL is saved in `~/.config/smarter-compact/proxy-backup.json` and restored by `sc disable claude`. Logs: `~/.config/smarter-compact/proxy.log`. Set `SMARTER_COMPACT_AUTO_PROXY=0` to disable auto-start.

### Codex

```
git clone https://github.com/bcmyguest/smarter-compact.git
codex plugin marketplace add "$PWD/smarter-compact"
codex plugin add smarter-compact@smarter-compact
./plugin/scripts/sc enable codex
```

`enable` sets `model_provider = "smarter-compact"` in `~/.codex/config.toml`. Codex keeps its ChatGPT login: the provider uses `requires_openai_auth` and LiteLLM's `/backend-api/codex` pass-through. Trust the hooks, quit Codex, and start it again.

### Copilot CLI

```
git clone https://github.com/bcmyguest/smarter-compact.git
copilot plugin marketplace add "$PWD/smarter-compact"
copilot plugin install smarter-compact@smarter-compact
```

The plugin registers the search tools and the compact hook. Copilot's model URL is not rewritten. Trimming on Copilot requires the CLI to send model traffic through the proxy yourself.

### Pi

```
pi install git:github.com/bcmyguest/smarter-compact
```

`/smarter-compact status` and `/smarter-compact compact` call the same command as the other agents. Pi does not rewrite the outgoing model request, so a trim happens only when that request already goes through the proxy.

### Cursor Agent

```
python scripts/build_plugin.py --out ~/.cursor/plugins/local/
```

Reload Cursor and confirm `smarter-compact` appears under local plugins. Cursor's hooks record a compact request and the MCP server serves search. Cursor does not rewrite the model request from a hook.

### OpenCode

```sh
git clone https://github.com/bcmyguest/smarter-compact.git
cd smarter-compact
opencode plugin smarter-compact        # project .opencode/opencode.json
opencode plugin -g smarter-compact       # global ~/.config/opencode/
```

**OpenCode 2** trims in the plugin (`context` hook). Add options on the plugin entry if you need them; see [docs/opencode.md](docs/opencode.md).

**OpenCode 1** has no context hook, so run `./plugin/scripts/sc enable opencode` as well (the plugin starts the proxy when the agent is pointed at it). `enable opencode` refuses a proxy base URL when `opencode --version` reports major 2.

## CLI

After `npm install -g smarter-compact`, the command on your PATH is **`sc`** (short) or **`smarter-compact`** (same binary). From a clone, use `./plugin/scripts/sc` or `./plugin/scripts/smarter-compact`.

The npm **package** name is `smarter-compact` (the registry name `sc` is taken by an unrelated Sauce Labs package). A global install only adds our `sc`; it does not replace other tools unless your shell picks this one first.

**Name collision:** on some Linux distros, `apt install sc` installs the classic terminal spreadsheet **`sc`**. That binary is uncommon on developer machines (nothing on PATH on a typical Ubuntu desktop unless you install it). Run `command -v sc` before relying on the short name; use `smarter-compact` if another `sc` wins.

## Commands

| Command | Effect |
| --- | --- |
| `/smarter-compact:status` | Trims, estimated tokens saved, and fail-opens in the last day |
| `/smarter-compact:compact` | Trim on the next request. The hook writes `sessions/<id>.force` |
| `sc enable <claude\|codex\|opencode>` | Point that agent at the proxy |
| `sc disable <claude\|codex\|opencode>` | Remove that pointing and restore the backup |
| `sc proxy` | Run LiteLLM in the foreground (debugging; hooks normally start it detached) |
| `sc status` | Same numbers as the slash command |

A fail-open sends the request on untrimmed and records the error. The request is not blocked.

## Where state lives

`OPENMEM_STATE_DIR` defaults to `~/.local/share/smarter-compact` for smarter-compact and the plugin. The store is `$OPENMEM_STATE_DIR/context`. Schema and file layout: [docs/context-store.md](docs/context-store.md).

| Variable | Default | Effect |
| --- | --- | --- |
| `CONTEXT_PROXY_ENABLED` | `1` | `0` forwards every request unchanged |
| `CONTEXT_TRIM_AT_TOKENS` | unset: 95000 for Haiku, 125000 otherwise | Tokens that trigger a trim |
| `CONTEXT_TRIM_TO_FRACTION` | `0.3` | In-turn tool steps kept while the request stays under this fraction of the threshold |
| `CONTEXT_COMPRESS_OVER_TOKENS` | `4000` | Tool results above this are stored in full and replaced by a short header |
| `CONTEXT_COMPACTION_RECALL` | `0` by default | `1` asks a memory bank named on the session registration |
| `CONTEXT_PROXY_PORT` | `4101` | LiteLLM listen port (env overrides `~/.config/smarter-compact/proxy.env`; `sc enable` writes the port in use) |

### Proxy port

Default is **4101**. To use another port:

```sh
export CONTEXT_PROXY_PORT=4200
sc enable claude    # agent URLs and proxy.env both use 4200
```

Or set it once in `~/.config/smarter-compact/proxy.env` (same file as the LiteLLM key), then run `sc enable` again so agent configs pick up the port:

```
CONTEXT_PROXY_PORT=4200
```

OpenCode 2 plugin options: `"proxyPort": 4200` in the plugin entry (maps to `CONTEXT_PROXY_PORT` for in-process trim settings; proxy auto-start uses the same resolution).

## Tests

```
python3 -m unittest discover -s proxy/tests
node --test tests/plugin.test.mjs
```

The Python package is still `openmem_context`. `plugin/lib/context-trim.mjs` is the OpenCode port of that package; the two stay on the same store contract.

## License

Apache-2.0. The trimmer comes from [openmem](https://github.com/bcmyguest/openmem).
