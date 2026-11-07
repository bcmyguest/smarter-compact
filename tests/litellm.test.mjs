import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { ensureLitellm, litellmProblem, litellmVersion, LITELLM_SPEC, whichOnPath } from "../plugin/lib/litellm.mjs";

function tmpDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function stub(dir, name, body) {
  const file = path.join(dir, name);
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return file;
}

function litellmStub(dir, version) {
  return stub(dir, "litellm", `echo ""; echo "LiteLLM: Current Version = ${version}"; echo ""`);
}

/** uv that logs its argv and, on `tool install`, drops a litellm reporting `version` into `bin`. */
function uvStub(dir, { bin, version = "1.109.0rc1", status = 0 }) {
  const log = path.join(dir, "uv.log");
  stub(dir, "uv", [
    `echo "$*" >> ${log}`,
    `if [ "$1 $2" = "tool dir" ]; then echo ${bin}; exit 0; fi`,
    `if [ "$1 $2" = "tool install" ]; then`,
    `  printf '#!/bin/sh\\necho "LiteLLM: Current Version = ${version}"\\n' > ${bin}/litellm`,
    `  /bin/chmod 755 ${bin}/litellm`,
    `  exit ${status}`,
    `fi`,
  ].join("\n"));
  return log;
}

test("litellmVersion parses --version and is null when litellm is absent", () => {
  const bin = tmpDir("sc-ll-");
  assert.equal(litellmVersion({ PATH: bin }), null);
  litellmStub(bin, "1.109.0.dev1");
  assert.equal(litellmVersion({ PATH: bin }), "1.109.0.dev1");
});

test("ensureLitellm keeps a current litellm and never calls uv", () => {
  const bin = tmpDir("sc-ll-");
  litellmStub(bin, "1.109.0rc1");
  const log = uvStub(bin, { bin });
  const r = ensureLitellm({ env: { PATH: bin } });
  assert.deepEqual(r, { ok: true, status: "present", version: "1.109.0rc1" });
  assert.equal(fs.existsSync(log), false);
});

test("ensureLitellm fails with uv-missing when litellm is old and uv absent", () => {
  const bin = tmpDir("sc-ll-");
  litellmStub(bin, "1.105.1");
  const r = ensureLitellm({ env: { PATH: bin } });
  assert.equal(r.ok, false);
  assert.equal(r.reason, "uv-missing");
  assert.equal(r.found, "1.105.1");
  assert.match(litellmProblem(r, "smarter-compact"), /found 1\.105\.1.*uv is missing/);
});

test("ensureLitellm installs with uv when litellm is missing", () => {
  const bin = tmpDir("sc-ll-");
  const log = uvStub(bin, { bin });
  const lines = [];
  const r = ensureLitellm({ env: { PATH: bin }, log: (l) => lines.push(l) });
  assert.deepEqual(r, { ok: true, status: "installed", version: "1.109.0rc1" });
  assert.equal(fs.readFileSync(log, "utf8"), `tool install --force ${LITELLM_SPEC}\n`);
  assert.match(lines[0], /litellm not on PATH/);
});

test("ensureLitellm upgrades an old litellm in uv's bin dir", () => {
  const bin = tmpDir("sc-ll-");
  litellmStub(bin, "1.105.1");
  uvStub(bin, { bin, version: "1.109.0.dev2" });
  assert.deepEqual(ensureLitellm({ env: { PATH: bin } }), { ok: true, status: "installed", version: "1.109.0.dev2" });
});

test("ensureLitellm reports install-failed when uv exits non-zero", () => {
  const bin = tmpDir("sc-ll-");
  uvStub(bin, { bin, status: 1 });
  assert.equal(ensureLitellm({ env: { PATH: bin } }).reason, "install-failed");
});

test("ensureLitellm reports shadowed when an older litellm comes first on PATH", () => {
  const first = tmpDir("sc-ll-first-");
  const uvBin = tmpDir("sc-ll-uv-");
  litellmStub(first, "1.105.1");
  uvStub(uvBin, { bin: uvBin });
  const r = ensureLitellm({ env: { PATH: [first, uvBin].join(path.delimiter) } });
  assert.equal(r.reason, "shadowed");
  assert.equal(r.path, whichOnPath("litellm", { PATH: first }));
  assert.equal(r.uvBin, uvBin);
});

test("ensureLitellm reports not-on-path when uv's bin dir is off PATH", () => {
  const bin = tmpDir("sc-ll-");
  const uvBin = tmpDir("sc-ll-uv-");
  uvStub(bin, { bin: uvBin });
  const r = ensureLitellm({ env: { PATH: bin } });
  assert.equal(r.reason, "not-on-path");
  assert.equal(r.uvBin, uvBin);
});
