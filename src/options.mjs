/** Map OpenCode plugin `options` to CONTEXT_* env vars trimSettings already reads. */
export function envFromPluginOptions(options = {}, base = process.env) {
  const env = { ...base };
  const set = (key, value) => {
    if (value === undefined || value === null) return;
    env[key] = String(value);
  };
  const flag = (key, on) => {
    if (on === undefined) return;
    env[key] = on ? "1" : "0";
  };
  flag("CONTEXT_PROXY_ENABLED", options.enabled);
  flag("CONTEXT_STANDING_INSTRUCTION", options.standingInstruction);
  set("CONTEXT_TRIM_AT_TOKENS", options.trimAtTokens);
  set("CONTEXT_TRIM_TO_TOKENS", options.trimToTokens);
  set("CONTEXT_COMPRESS_OVER_TOKENS", options.compressOverTokens);
  set("CONTEXT_CHARS_PER_TOKEN", options.charsPerToken);
  set("SMARTER_COMPACT_STATE_DIR", options.stateDir);
  set("CONTEXT_PROXY_PORT", options.proxyPort);
  return env;
}
