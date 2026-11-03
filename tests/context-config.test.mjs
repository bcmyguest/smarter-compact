import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { applyTrimConfigToEnv, listTrimConfig, runContextCommand, setTrimAssignment } from "../plugin/lib/context-config.mjs";
import { trimAtFor, trimSettings } from "../plugin/lib/context-trim.mjs";
import { ensureProductEnv } from "../plugin/lib/brand.mjs";

function tmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "sc-context-"));
}

test("context set haiku persists and applies to trimAtFor", () => {
  const home = tmpHome();
  setTrimAssignment(home, { key: "haiku", tokens: 88000 });
  const env = { HOME: home };
  applyTrimConfigToEnv(env, home);
  assert.equal(env.CONTEXT_TRIM_AT_OVERRIDES, JSON.stringify({ haiku: 88000 }));
  const settings = trimSettings(env, home);
  assert.equal(trimAtFor(settings, "claude-haiku-5-5"), 88000);
  assert.equal(trimAtFor(settings, "claude-sonnet-5"), 125000);
});

test("global trim in config sets CONTEXT_TRIM_AT_TOKENS", () => {
  const home = tmpHome();
  setTrimAssignment(home, { key: "trim", tokens: 99000 });
  const env = { HOME: home };
  applyTrimConfigToEnv(env, home);
  assert.equal(env.CONTEXT_TRIM_AT_TOKENS, "99000");
  assert.equal(env.CONTEXT_TRIM_AT_OVERRIDES, undefined);
});

test("ensureProductEnv loads trim table from config.toml", () => {
  const home = tmpHome();
  setTrimAssignment(home, { key: "default", tokens: 130000 });
  const env = { HOME: home };
  ensureProductEnv(env, home);
  const settings = trimSettings(env, home);
  assert.equal(trimAtFor(settings, "claude-sonnet-5"), 130000);
});

test("context list runs", () => {
  const home = tmpHome();
  setTrimAssignment(home, { key: "haiku", tokens: 88000 });
  const lines = [];
  assert.equal(runContextCommand(["list"], { home, log: { log: (m) => lines.push(m), error: () => {} } }), 0);
  assert.ok(lines.some((l) => l.includes("haiku: 88000")));
});

test("listTrimConfig empty home", () => {
  const home = tmpHome();
  assert.deepEqual(listTrimConfig(home), { global: null, families: {} });
});
