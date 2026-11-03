import assert from "node:assert/strict";
import test from "node:test";
import plugin, { PLUGIN_ID, server, setup } from "../index.mjs";

test("default export matches opencode-clm shape", () => {
  assert.equal(plugin.id, PLUGIN_ID);
  assert.equal(plugin.id, "opencode-shelf");
  assert.equal(typeof plugin.setup, "function");
  assert.equal(typeof plugin.server, "function");
  assert.equal(setup, plugin.setup);
  assert.equal(server, plugin.server);
});
