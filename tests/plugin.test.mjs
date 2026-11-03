import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { configureAgent } from "../plugin/lib/enable.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK = path.join(ROOT, "plugin/scripts/shelf-hook.mjs");
const CLI = path.join(ROOT, "plugin/scripts/shelf.mjs");

function tmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "shelf-"));
}

test("session start returns the standing note", () => {
  const home = tmpHome();
  const state = path.join(home, "state");
  const result = spawnSync(process.execPath, [HOOK], {
    input: JSON.stringify({ hook_event_name: "SessionStart", session_id: "s1", cwd: home }),
    encoding: "utf8",
    env: { ...process.env, HOME: home, OPENMEM_STATE_DIR: state },
  });
  assert.equal(result.status, 0, result.stderr);
  const out = JSON.parse(result.stdout);
  assert.match(out.hookSpecificOutput.additionalContext, /smart_compact_search/);
  assert.equal(fs.existsSync(path.join(state, "context", "sessions", "s1.json")), true);
});

test("compact prompt writes the force flag", () => {
  const home = tmpHome();
  const state = path.join(home, "state");
  const result = spawnSync(process.execPath, [HOOK], {
    input: JSON.stringify({
      hook_event_name: "UserPromptSubmit",
      session_id: "s2",
      prompt: "<!-- openmem:smart-compact --> trim",
    }),
    encoding: "utf8",
    env: { ...process.env, HOME: home, OPENMEM_STATE_DIR: state },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(fs.existsSync(path.join(state, "context", "sessions", "s2.force")), true);
});

test("enable and disable claude rewrite only our settings", () => {
  const home = tmpHome();
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  fs.writeFileSync(path.join(home, ".claude", "settings.json"), JSON.stringify({ env: { ANTHROPIC_BASE_URL: "https://example.test" }, keep: 1 }));
  const on = configureAgent(home, "claude", { enabled: true, port: 4101 });
  assert.equal(on.ok, true);
  const settings = JSON.parse(fs.readFileSync(path.join(home, ".claude", "settings.json"), "utf8"));
  assert.equal(settings.keep, 1);
  assert.equal(settings.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:4101");
  assert.match(settings.env.ANTHROPIC_CUSTOM_HEADERS, /x-litellm-api-key: Bearer /);
  const off = configureAgent(home, "claude", { enabled: false, port: 4101 });
  assert.equal(off.ok, true);
  const restored = JSON.parse(fs.readFileSync(path.join(home, ".claude", "settings.json"), "utf8"));
  assert.equal(restored.env.ANTHROPIC_BASE_URL, "https://example.test");
  assert.equal(restored.env.ANTHROPIC_CUSTOM_HEADERS, undefined);
});

test("enable codex writes a provider and keeps a foreign model_provider for restore", () => {
  const home = tmpHome();
  fs.mkdirSync(path.join(home, ".codex"), { recursive: true });
  fs.writeFileSync(path.join(home, ".codex", "config.toml"), 'model = "gpt"\nmodel_provider = "openai"\n');
  const on = configureAgent(home, "codex", { enabled: true, port: 4101 });
  assert.equal(on.ok, true, on.message);
  const text = fs.readFileSync(path.join(home, ".codex", "config.toml"), "utf8");
  assert.match(text, /model = "gpt"/);
  assert.match(text, /model_provider = "shelf"/);
  assert.match(text, /\[model_providers\.shelf\]/);
  assert.match(text, /127\.0\.0\.1:4101\/backend-api\/codex/);
  const off = configureAgent(home, "codex", { enabled: false, port: 4101 });
  assert.equal(off.ok, true, off.message);
  const restored = fs.readFileSync(path.join(home, ".codex", "config.toml"), "utf8");
  assert.match(restored, /model_provider = "openai"/);
  assert.doesNotMatch(restored, /model_providers\.shelf/);
});

test("opencode 2 is not given a proxy base URL", () => {
  const home = tmpHome();
  const result = configureAgent(home, "opencode", { enabled: true, port: 4101, major: 2 });
  assert.equal(result.ok, false);
  assert.match(result.message, /OpenCode 2/);
  assert.equal(fs.existsSync(path.join(home, ".config", "opencode", "opencode.json")), false);
});

test("status runs against an empty store", () => {
  const home = tmpHome();
  const result = spawnSync(process.execPath, [CLI, "status"], {
    encoding: "utf8",
    env: { ...process.env, HOME: home, OPENMEM_STATE_DIR: path.join(home, "state") },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /trims: 0/);
});
