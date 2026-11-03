# OpenCode

Install matches [opencode-clm](https://github.com/bcmyguest/opencode-clm): one npm package, `opencode plugin`, one default export with `server` (OpenCode 1) and `setup` (OpenCode 2).

## Install

```sh
opencode plugin smarter-compact        # this project: .opencode/opencode.json
opencode plugin -g smarter-compact     # global: ~/.config/opencode/opencode.json(c)
```

From a clone before publish:

```sh
opencode plugin "$PWD"                  # marketplace root with .claude-plugin/marketplace.json, or
opencode plugin ./index.mjs             # when your OpenCode build accepts a file path
```

Keep the clone in place; OpenCode resolves the package from it.

## OpenCode 2

Add the plugin to server config only. Options are the same keys as `CONTEXT_*` env vars (camelCase in JSON):

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
- MCP tools `smart_compact_search`, `smart_compact_sql`, `smart_compact_get`, `context_status` — register the plugin MCP entry or run `plugin/scripts/smarter-compact-mcp` from a clone.

## OpenCode 1

OpenCode 1 has no context hook, so trimming goes through the LiteLLM proxy:

```sh
./plugin/scripts/smarter-compact enable opencode
./plugin/scripts/smarter-compact proxy
```

Plugin options in `opencode.json`:

```jsonc
{ "plugin": [["smarter-compact", { "trimAtTokens": 125000 }]] }
```

The `server` hook sends `x-openmem-session-id` when your Anthropic `baseURL` is the local proxy, registers the session, and honours `/compact`.

## Options

| option | env | default |
| --- | --- | --- |
| `enabled` | `CONTEXT_PROXY_ENABLED` | on |
| `standingInstruction` | `CONTEXT_STANDING_INSTRUCTION` | on |
| `trimAtTokens` | `CONTEXT_TRIM_AT_TOKENS` | 125000 (95000 for Haiku when unset) |
| `trimToFraction` | `CONTEXT_TRIM_TO_FRACTION` | 0.3 |
| `compressOverTokens` | `CONTEXT_COMPRESS_OVER_TOKENS` | 4000 |
| `charsPerToken` | `CONTEXT_CHARS_PER_TOKEN` | 2.5 |
| `compactionMaxChars` | `CONTEXT_COMPACTION_MAX_CHARS` | 12000 |
| `compactionRecall` | `CONTEXT_COMPACTION_RECALL` | off in Smarter Compact |
| `stateDir` | `OPENMEM_STATE_DIR` | `~/.local/share/smarter-compact` |

Store layout: [context-store.md](context-store.md).
