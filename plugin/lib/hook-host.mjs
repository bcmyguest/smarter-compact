/** Which agent invoked smarter-compact-hook, and whether that path can trim on the wire. */

/** Hooks-only integrations: MCP + session registration; no proxy URL rewrite. */
export const MCP_ONLY_HOSTS = new Set(["cursor", "copilot"]);

export function hookHost(argv = process.argv, msg = {}) {
  const i = argv.indexOf("--host");
  const fromArgv = i >= 0 ? String(argv[i + 1] || "").toLowerCase() : "";
  const fromMsg = String(msg.host || "").toLowerCase();
  return fromArgv || fromMsg || "agent";
}

export function isMcpOnlyHost(host) {
  return MCP_ONLY_HOSTS.has(String(host).toLowerCase());
}

/** True when SessionStart should start the proxy and /compact can affect the next model request. */
export function trimsOnWire(host) {
  return !isMcpOnlyHost(host);
}
