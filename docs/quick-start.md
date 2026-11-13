# Quick start

Install from npm with `npm install -g smarter-compact`, then register plugins with **`smarter-compact plugins add <claude|codex>`**. It finds the installed package itself, so you need neither `npm root -g` nor a git clone. **`smarter-compact enable -g`** detects Claude Code, Codex, and OpenCode (by config dir, or `opencode` on PATH), points each at the proxy, and starts the proxy. It prints each step to stderr; `-q` (or `SMARTER_COMPACT_QUIET=1`) prints only errors and warnings. Clone the repo only to develop the trimmer or to pin a checkout path.

## Claude Code

```sh
npm install -g smarter-compact
smarter-compact plugins add claude
smarter-compact enable claude
```

Start a new `claude` session after `enable`. The marketplace install registers the search tools and the compact command. `enable` points `ANTHROPIC_BASE_URL` at `http://127.0.0.1:4101`, starts the LiteLLM proxy when needed (a Docker container that restarts with Docker after a reboot, or without Docker a background process the next session restarts; see [runtime](context-proxy.md#runtime)), and stores a key in `~/.config/smarter-compact/proxy.env` (mode 600). Session hooks also ensure the proxy is up. `enable` saves any previous base URL in `~/.config/smarter-compact/proxy-backup.json`, and `smarter-compact disable claude` restores it. Logs: `docker logs smarter-compact-proxy`, or `~/.config/smarter-compact/proxy.log` without Docker. Set `SMARTER_COMPACT_AUTO_PROXY=0` to disable auto-start.

**Remote Control.** Claude Code refuses [Remote Control](https://code.claude.com/docs/en/remote-control) while `ANTHROPIC_BASE_URL` points anywhere but `api.anthropic.com` ("Remote Control is only available when using Claude via api.anthropic.com"). The `env` block in `~/.claude/settings.json` overrides the shell, so `unset` does not help. Run `smarter-compact disable claude` before `claude remote-control` or `/remote-control`, and `smarter-compact enable claude` afterwards. Sessions started while disabled bypass the proxy and are not trimmed.

On Claude Code 2.1.275 or later you can install the plugin from inside Claude Code instead of running `plugins add`:

```
/plugin install smarter-compact --marketplace bcmyguest/smarter-compact
```

That adds the search tools, hooks and commands. Trimming still needs `smarter-compact enable claude`, which points Claude Code at the proxy.

<details>
<summary>Optional: install from a git clone</summary>

```sh
git clone https://github.com/bcmyguest/smarter-compact.git
cd smarter-compact
./plugin/scripts/smarter-compact plugins add claude
./plugin/scripts/smarter-compact enable claude
```

Leave the clone in place; Claude resolves the marketplace from that path.

</details>

## Codex

```sh
npm install -g smarter-compact
smarter-compact plugins add codex
smarter-compact enable codex
```

`enable` sets `model_provider = "smarter-compact"` in `~/.codex/config.toml`. Codex keeps its ChatGPT login: the provider uses `requires_openai_auth` and LiteLLM's `/backend-api/codex` pass-through. Trust the hooks, quit Codex, and start it again.

<details>
<summary>Optional: install from a git clone</summary>

```sh
git clone https://github.com/bcmyguest/smarter-compact.git
cd smarter-compact
./plugin/scripts/smarter-compact plugins add codex
./plugin/scripts/smarter-compact enable codex
```

</details>

## Pi

```sh
pi install git:github.com/bcmyguest/smarter-compact
```

Pi loads `plugin/pi/smarter-compact.js`, which registers on Pi's **`context` hook** (documented on [pi.dev](https://pi.dev/docs/latest/extensions#context)) and runs the same in-process trim as OpenCode 2. Configure thresholds with `smarter-compact context set …` or `~/.config/smarter-compact/config.toml`. Command: `/smarter-compact compact` forces a trim on the next request.

<details>
<summary>Optional: git clone (local extension path)</summary>

```sh
git clone https://github.com/bcmyguest/smarter-compact.git
# Point Pi at smarter-compact/plugin/pi/smarter-compact.js per Pi's local-extension docs.
```

</details>

## OpenCode

```sh
npm install -g smarter-compact
opencode plugin smarter-compact        # project .opencode/opencode.json
opencode plugin -g smarter-compact   # global ~/.config/opencode/
```

**OpenCode 2** trims in the plugin (`context` hook). Add options on the plugin entry if you need them; see [opencode.md](opencode.md).

**OpenCode 1** has no context hook, so run `smarter-compact enable opencode` as well (the plugin starts the proxy when the agent is pointed at it). `enable opencode` refuses a proxy base URL when `opencode --version` reports major 2.

<details>
<summary>Optional: install from a git clone</summary>

```sh
git clone https://github.com/bcmyguest/smarter-compact.git
cd smarter-compact
opencode plugin "$PWD"               # or ./index.mjs if your OpenCode build accepts a file path
opencode plugin -g "$PWD"
smarter-compact enable opencode                   # OpenCode 1 only
```

</details>

## Upgrade

Claude Code, Codex and OpenCode 1 send requests through the background proxy, which keeps running the version that started it. OpenCode 2 and Pi trim in-process and pick up the new version on their next start.

```sh
npm install -g smarter-compact@latest
smarter-compact proxy restart                                   # load the new proxy now
claude plugin marketplace update smarter-compact                # Claude Code: refresh the plugin,
claude plugin update smarter-compact@smarter-compact            # then restart claude
```

Skip `proxy restart` and the next session start restarts the proxy once it has been idle 60 s, showing `proxy restarted, <old> -> <new>`. A proxy that is busy, or that `smarter-compact` did not start, gets a warning naming what to restart instead. `proxy restart` stops only a litellm that `smarter-compact` started, or a Docker container publishing the port (its own `smarter-compact-proxy` container is recreated from the installed package); it works on Linux, macOS and Windows. See [Updates](context-proxy.md#updates) for the rules.
