import assert from "node:assert/strict";
import test from "node:test";

import {
  expectedBin,
  requiredRuntimeFiles,
  validatePackageContents,
} from "../scripts/check_package.mjs";

const completeFiles = [...requiredRuntimeFiles];
const completeManifest = {
  bin: {
    [expectedBin.name]: expectedBin.target,
  },
};

function validateWithout(path) {
  return validatePackageContents(
    completeFiles.filter((file) => file !== path),
    completeManifest,
  );
}

test("accepts a package containing the complete Node and skill runtime", () => {
  assert.deepEqual(validatePackageContents(completeFiles, completeManifest), {
    binErrors: [],
    forbiddenFiles: [],
    missingRuntimeFiles: [],
  });
});

test("requires the packaged CLI target and matching package.json bin entry", () => {
  assert.deepEqual(validateWithout(expectedBin.target).missingRuntimeFiles, [expectedBin.target]);

  const result = validatePackageContents(completeFiles, {
    bin: { [expectedBin.name]: "bin/missing.js" },
  });
  assert.equal(result.binErrors.length, 1);
  assert.match(result.binErrors[0], /bin\/open-kimi-ppt-skills\.js/);
});

test("requires the editor server and browser runtime entry and assets", () => {
  for (const path of [
    "lib/editor-server.js",
    "editor/index.html",
    "editor/app.js",
    "editor/lib.js",
    "editor/styles.css",
  ]) {
    assert.deepEqual(validateWithout(path).missingRuntimeFiles, [path]);
  }
});

test("requires requirements and every critical skill runtime file", () => {
  for (const path of [
    "skills/open-kimi-ppt/SKILL.md",
    "skills/open-kimi-ppt/requirements.txt",
    "skills/open-kimi-ppt/reference/pptd.md",
    "skills/open-kimi-ppt/reference/slides_categories.md",
    "skills/open-kimi-ppt/scripts/browser_export.py",
    "skills/open-kimi-ppt/scripts/export_host.html",
    "skills/open-kimi-ppt/scripts/export_images.py",
    "skills/open-kimi-ppt/scripts/export_pptx.py",
  ]) {
    assert.deepEqual(validateWithout(path).missingRuntimeFiles, [path]);
  }
});

test("continues to reject sensitive and generated package files", () => {
  const result = validatePackageContents(
    [...completeFiles, "browser-profile/Default/Cookies", "output.pptx"],
    completeManifest,
  );
  assert.deepEqual(result.forbiddenFiles, [
    "browser-profile/Default/Cookies",
    "output.pptx",
  ]);
});
