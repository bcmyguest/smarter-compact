import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { opencodeMajorVersion } from "../plugin/lib/opencode-config.mjs";
import { addPluginMarketplace } from "../plugin/lib/plugins-cli.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const SHIPPED = ["plugin/lib", "plugin/scripts", "plugin/pi", "src"].map((d) => path.join(ROOT, d)).concat(path.join(ROOT, "index.mjs"));
const CALL_RE = /\b(spawn|spawnSync|execFile)\(/g;

function sourceFiles() {
  const out = [];
  const walk = (p) => {
    if (fs.statSync(p).isDirectory()) fs.readdirSync(p).forEach((n) => walk(path.join(p, n)));
    else if (/\.(mjs|js)$/.test(p)) out.push(p);
  };
  SHIPPED.forEach(walk);
  return out;
}

/** Text of the call starting at `open` (the index of its `(`), up to the matching `)`; skips string literals. */
function callText(src, open) {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '"' || c === "'" || c === "`") {
      for (i++; i < src.length && src[i] !== c; i++) if (src[i] === "\\") i++;
    } else if (c === "(") depth++;
    else if (c === ")" && --depth === 0) return src.slice(open, i + 1);
  }
  throw new Error("unbalanced call");
}

// Issue #1: on Windows a child without windowsHide opens a console window; closing it kills the proxy.
test("every child_process call in shipped code sets windowsHide (#1)", () => {
  const calls = [];
  for (const file of sourceFiles()) {
    const src = fs.readFileSync(file, "utf8");
    for (const m of src.matchAll(CALL_RE)) {
      if (/function\s*$/.test(src.slice(0, m.index))) continue;
      calls.push({ at: `${path.relative(ROOT, file)}:${src.slice(0, m.index).split("\n").length}`, text: callText(src, m.index + m[0].length - 1) });
    }
  }
  assert.ok(calls.length >= 10, `scan found ${calls.length} calls; expected at least 10`);
  const missing = calls.filter((c) => !/windowsHide:\s*true/.test(c.text)).map((c) => c.at);
  assert.deepEqual(missing, []);
});

test("opencodeMajorVersion passes windowsHide to spawn (#1)", () => {
  let opts;
  opencodeMajorVersion({ spawn: (_bin, _args, o) => { opts = o; return { status: 0, stdout: "2.0.0" }; } });
  assert.equal(opts.windowsHide, true);
});

test("addPluginMarketplace passes windowsHide to every step (#1)", () => {
  const seen = [];
  addPluginMarketplace("claude", ROOT, { spawn: (_bin, _args, o) => { seen.push(o.windowsHide); return { status: 0 }; } });
  assert.ok(seen.length > 0);
  assert.ok(seen.every((v) => v === true));
});
