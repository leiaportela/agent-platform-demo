import { test } from "node:test";
import assert from "node:assert/strict";
import { hashForView, viewFromHash } from "./views.js";

test("chat is the default view", () => {
  for (const h of ["", "#", "#/", "#/nope", undefined, null]) assert.equal(viewFromHash(h), "chat");
});

test("architecture view from its hash, with or without slash or query", () => {
  for (const h of ["#/architecture", "#architecture", "#/Architecture", "#/architecture?x=1", "#/architecture/"]) {
    assert.equal(viewFromHash(h), "architecture");
  }
});

test("hash round-trips", () => {
  for (const v of ["chat", "architecture"]) assert.equal(viewFromHash(hashForView(v)), v);
});
