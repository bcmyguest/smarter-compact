# OpenCode

The package default export provides `setup` (OpenCode 2 context hook and MCP self-registration) and `server` (OpenCode 1 headers and `/compact`).

## Install

OpenCode 2 (`opencode` >= 2.2):

```sh
opencode plugin add smarter-compact            # npm package (recommended)
opencode plugin add <user>/smarter-compact     # Git shorthand
opencode plugin list                            # installed package plugins
```

Both write `"plugins": ["<spec>"]` to the global config and install the package under the OpenCode npm cache.

From a local clone, `plugin add` accepts only npm/Git specs; point the config at the checkout instead:

```jsonc
{ "plugins": ["/absolute/path/to/smarter-compact"] }
```

Keep the clone in place; OpenCode resolves the package from it.

## OpenCode 2

The plugin registers its context hooks by itself, and *attempts* to register its search MCP
tools the same way (`ctx.mcp.add`, OpenCode >= 2.2). That attempt can end without a connected
server — the call is accepted and nothing connects — so **write the `mcp` entry below as well**;
it is the configuration to rely on, and registering twice is harmless. Options are the same keys
as `CONTEXT_*` env vars (camelCase in JSON):

```jsonc
// opencode.json
{
  "plugins": [
    {
      "package": "smarter-compact",
      "options": {
        "trimAtTokens": 125000,
        "compressOverTokens": 4000,
        "compactionRecall": false
      }
    }
  ]
}
```

OpenCode 2 trims inside the plugin. You do not run `smarter-compact enable opencode` or point `provider.anthropic.options.baseURL` at the proxy.

Commands:

- `/compact` — trim on the next request (registered by the plugin).
- MCP tools `smart_compact_search`, `smart_compact_sql`, `smart_compact_get`, `context_status` (needs `node` >= 24 on `PATH`). Configure them explicitly — the plugin's own `ctx.mcp.add` is best effort:

```jsonc
// opencode.json — same store the plugin trims into, so $SMARTER_COMPACT_STATE_DIR must match
{
  "mcp": {
    "servers": {
      "smarter-compact": {
        "type": "local",
        "command": ["node", "/path/to/smarter-compact/plugin/lib/mcp/server.mjs"],
        "environment": { "SMARTER_COMPACT_STATE_DIR": "/var/lib/smarter-compact" },
        "enabled": true
      }
    }
  }
}
```

Verify rather than assume: the plugin prints one `smarter-compact: recall MCP registered` (or
`NOT registered reason=…`) line to stderr at startup, and every answered call appends a line to
`$SMARTER_COMPACT_STATE_DIR/context/tool-calls.jsonl`. OpenCode exposes an MCP tool as
`<server>_<tool>` — `smarter-compact_smart_compact_search`, underscore, seen verbatim in captured
requests — so the pointer and standing instruction name that qualified form, never the bare one.
When a server is left behind code mode the tools are reachable only through that runner's
`execute` input, so counting top-level tool names alone under-reads recall to zero; the bench
therefore sets `"codemode": false` on this server.

## OpenCode 1

OpenCode 1 has no context hook, so trimming goes through the LiteLLM proxy:

```sh
./plugin/scripts/smarter-compact enable opencode
```

The plugin starts the proxy in the background when needed (`CONTEXT_PROXY_PORT`, default 4101).

Plugin options in `opencode.json`:

```jsonc
{ "plugin": [["smarter-compact", { "trimAtTokens": 125000 }]] }
```

The `server` hook sends `x-smarter-compact-session-id` when your Anthropic `baseURL` is the local proxy, registers the session, and honours `/compact`.

## Options

| option | env | default |
| --- | --- | --- |
| `enabled` | `CONTEXT_PROXY_ENABLED` | on |
| `standingInstruction` | `CONTEXT_STANDING_INSTRUCTION` | on |
| `trimAtTokens` | `CONTEXT_TRIM_AT_TOKENS` | 125000 (95000 for Haiku when unset; setting this disables per-model defaults) |
| `trimToFraction` | `CONTEXT_TRIM_TO_FRACTION` | 0.3 |
| `compressOverTokens` | `CONTEXT_COMPRESS_OVER_TOKENS` | 4000 |
| `charsPerToken` | `CONTEXT_CHARS_PER_TOKEN` | 2.5 |
| `compactionMaxChars` | `CONTEXT_COMPACTION_MAX_CHARS` | 12000 |
| `compactionRecall` | `CONTEXT_COMPACTION_RECALL` | off in Smarter Compact |
| `stateDir` | `SMARTER_COMPACT_STATE_DIR` | `~/.local/share/smarter-compact` |
| `proxyPort` | `CONTEXT_PROXY_PORT` | 4101 (also read from `~/.config/smarter-compact/proxy.env`) |

Store layout: [context-store.md](context-store.md).
