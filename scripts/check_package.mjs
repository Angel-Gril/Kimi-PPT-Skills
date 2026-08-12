#!/usr/bin/env node

import { execFileSync, execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const forbiddenSegments = [
  /(^|\/)__pycache__(\/|$)/i,
  /(^|\/)browser-profile(\/|$)/i,
  /(^|\/)(?:local storage|session storage|indexeddb|service worker)(\/|$)/i,
  /(^|\/)(?:default|profile \d+)(\/|$)/i,
  /(^|\/)playwright-report(\/|$)/i,
  /(^|\/)test-results(\/|$)/i,
  /(^|\/)(?:downloads?|export-output|logs?|tmp|temp)(\/|$)/i,
  /(^|\/)\.open-kimi-ppt(\/|$)/i,
  /(^|\/)\.env(?:\.|$)/i,
];
const forbiddenExtensions = new Set([
  ".crdownload",
  ".db",
  ".db-shm",
  ".db-wal",
  ".key",
  ".ldb",
  ".log",
  ".p12",
  ".pem",
  ".pfx",
  ".ppt",
  ".pptx",
  ".pyc",
  ".pyd",
  ".pyo",
  ".sqlite",
  ".sqlite3",
  ".tgz",
  ".zip",
]);
const forbiddenNames = [
  /(^|\/).*token.*$/i,
  /(^|\/).*credentials.*$/i,
  /(^|\/)(?:cookies?|login data|web data|history|preferences|secure preferences)$/i,
  /(^|\/)(?:current|lock|log|manifest-\d+)$/i,
];

const expectedBin = {
  name: "open-kimi-ppt-skills",
  target: "bin/open-kimi-ppt-skills.js",
};

const requiredRuntimeFiles = new Set([
  "package.json",
  expectedBin.target,
  "lib/editor-server.js",
  "editor/index.html",
  "editor/app.js",
  "editor/lib.js",
  "editor/styles.css",
  "skills/open-kimi-ppt/SKILL.md",
  "skills/open-kimi-ppt/requirements.txt",
  "skills/open-kimi-ppt/reference/fonts.md",
  "skills/open-kimi-ppt/reference/general-poster.md",
  "skills/open-kimi-ppt/reference/pptd.md",
  "skills/open-kimi-ppt/reference/shapes.md",
  "skills/open-kimi-ppt/reference/slides_categories.md",
  "skills/open-kimi-ppt/reference/slides_categories/academic-research.md",
  "skills/open-kimi-ppt/reference/slides_categories/analysis-decision.md",
  "skills/open-kimi-ppt/reference/slides_categories/brand-creative.md",
  "skills/open-kimi-ppt/reference/slides_categories/business-plan.md",
  "skills/open-kimi-ppt/reference/slides_categories/education-training.md",
  "skills/open-kimi-ppt/reference/slides_categories/management-report.md",
  "skills/open-kimi-ppt/reference/slides_categories/tech-engineering.md",
  "skills/open-kimi-ppt/scripts/browser_export.py",
  "skills/open-kimi-ppt/scripts/export_host.html",
  "skills/open-kimi-ppt/scripts/export_images.py",
  "skills/open-kimi-ppt/scripts/export_pptx.py",
]);

function packageFiles() {
  const npmArguments = ["pack", "--dry-run", "--json", "--ignore-scripts"];
  const npmEntry = process.env.npm_execpath;
  const output = npmEntry
    ? execFileSync(process.execPath, [npmEntry, ...npmArguments], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      })
    : process.platform === "win32"
      ? execSync(`npm pack ${npmArguments.slice(1).join(" ")}`, {
          encoding: "utf8",
          stdio: ["ignore", "pipe", "pipe"],
        })
      : execFileSync("npm", npmArguments, {
          encoding: "utf8",
          stdio: ["ignore", "pipe", "pipe"],
        });
  const parsed = JSON.parse(output);
  if (!Array.isArray(parsed) || !parsed[0] || !Array.isArray(parsed[0].files)) {
    throw new Error("npm pack returned an unexpected manifest");
  }
  return parsed[0].files.map(({ path }) => String(path).replaceAll("\\", "/"));
}

function validatePackageContents(files, packageManifest) {
  const normalizedFiles = files.map((path) => String(path).replaceAll("\\", "/"));
  const missingRuntimeFiles = [...requiredRuntimeFiles].filter(
    (path) => !normalizedFiles.includes(path),
  );
  const configuredBin = packageManifest?.bin?.[expectedBin.name];
  const binErrors = configuredBin === expectedBin.target
    ? []
    : [
        `package.json bin.${expectedBin.name} must target ${expectedBin.target}; `
        + `found ${configuredBin ?? "nothing"}`,
      ];
  const forbiddenFiles = normalizedFiles.filter((path) => {
    const suffix = extname(path).toLowerCase();
    return (
      forbiddenExtensions.has(suffix) ||
      forbiddenSegments.some((pattern) => pattern.test(path)) ||
      forbiddenNames.some((pattern) => pattern.test(path))
    );
  });

  return { binErrors, forbiddenFiles, missingRuntimeFiles };
}

function reportValidation({ binErrors, forbiddenFiles, missingRuntimeFiles }, fileCount) {
  if (missingRuntimeFiles.length > 0) {
    console.error("Package-content check failed; missing runtime files:");
    for (const missing of missingRuntimeFiles) console.error(`  ${missing}`);
  }
  if (binErrors.length > 0) {
    console.error("Package-content check failed; invalid CLI configuration:");
    for (const error of binErrors) console.error(`  ${error}`);
  }
  if (forbiddenFiles.length > 0) {
    console.error("Package-content check failed; forbidden files:");
    for (const finding of forbiddenFiles) console.error(`  ${finding}`);
  }

  const failed = missingRuntimeFiles.length + binErrors.length + forbiddenFiles.length > 0;
  if (failed) {
    process.exitCode = 1;
  } else {
    console.log(`Package-content check passed (${fileCount} files).`);
  }
}

function main() {
  const files = packageFiles();
  const packageManifest = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  reportValidation(validatePackageContents(files, packageManifest), files.length);
}

export { expectedBin, requiredRuntimeFiles, validatePackageContents };

const invokedAsScript = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedAsScript) main();
