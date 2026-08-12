import assert from "node:assert/strict";
import test from "node:test";
import {
  assertWritableChangePath,
  extractPagePaths,
  joinDeckPath,
  normalizeRelativePath,
  titleFromManifest,
} from "../editor/lib.js";

const yaml = `---
version: v2
title: "测试文稿"
pages:
  - pages/01.page
  - 'pages/02.page' # comment
theme:
  colors: {}
`;

test("parses PPTD manifests and keeps file access inside the project", () => {
  assert.deepEqual(extractPagePaths(yaml), ["pages/01.page", "pages/02.page"]);
  assert.deepEqual(extractPagePaths('{"pages":["a.page","folder/b.page"]}'), ["a.page", "folder/b.page"]);
  assert.equal(titleFromManifest(yaml, "fallback"), "测试文稿");
  assert.equal(joinDeckPath("deck", "pages/01.page"), "deck/pages/01.page");
  assert.equal(joinDeckPath("deck", "deck/pages/01.page"), "deck/pages/01.page");
  assert.equal(assertWritableChangePath("deck", "pages/01.page"), "deck/pages/01.page");
  assert.throws(() => normalizeRelativePath("../secret"), /不允许越过/);
  assert.throws(() => normalizeRelativePath("/absolute/path"), /绝对路径/);
  assert.throws(() => assertWritableChangePath("deck", "media/image.png"), /仅允许编辑/);
});
