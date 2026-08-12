import assert from "node:assert/strict";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { installSkill } from "../bin/open-kimi-ppt-skills.js";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(projectRoot, "bin", "open-kimi-ppt-skills.js");

function runCli(args, env = process.env) {
  return spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    env,
  });
}

test("installs the packaged skill into a custom skills directory", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");

  try {
    const result = runCli(["install", "--target", target]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(existsSync(join(target, "open-kimi-ppt", "SKILL.md")), true);
    assert.equal(existsSync(join(target, "open-kimi-ppt", "scripts", "export_pptx.py")), true);
    assert.equal(existsSync(join(target, "open-kimi-ppt", "scripts", "export_host.html")), true);
    assert.equal(existsSync(join(target, "open-kimi-ppt", "_user_meta.json")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("installs into ~/.agents/skills when no target is provided", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));

  try {
    const result = runCli([], { ...process.env, HOME: root, USERPROFILE: root, CODEX_HOME: undefined });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(existsSync(join(root, ".agents", "skills", "open-kimi-ppt", "SKILL.md")), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("overwrites an existing installation by default", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const skillFile = join(target, "open-kimi-ppt", "SKILL.md");

  try {
    assert.equal(runCli(["--target", target]).status, 0);
    writeFileSync(skillFile, "modified", "utf8");

    const result = runCli(["--target", target]);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Updated open-kimi-ppt/);
    assert.match(readFileSync(skillFile, "utf8"), /^---\r?\nname: open-kimi-ppt/m);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("restores an existing installation when the staged rename fails", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const destination = join(target, "open-kimi-ppt");
  const skillFile = join(destination, "SKILL.md");

  try {
    mkdirSync(destination, { recursive: true });
    writeFileSync(skillFile, "previous installation", "utf8");
    let renameCount = 0;
    const operations = {
      renameSync(source, output) {
        renameCount += 1;
        if (renameCount === 2) throw new Error("injected install failure");
        return renameSync(source, output);
      },
    };

    assert.throws(
      () => installSkill({ target }, operations),
      /injected install failure/,
    );
    assert.equal(readFileSync(skillFile, "utf8"), "previous installation");
    assert.equal(
      readdirSync(target).some((name) => name.startsWith(".open-kimi-ppt-staging-")),
      false,
    );
    assert.equal(existsSync(join(target, ".open-kimi-ppt.backup")), false);
    assert.equal(existsSync(join(target, ".open-kimi-ppt.install.lock")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("waits for the atomic install lock before mutating the destination", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const lock = join(target, ".open-kimi-ppt.install.lock");
  let lockAttempts = 0;
  let sleeps = 0;

  try {
    installSkill(
      { target, lockTimeoutMs: 100, lockRetryMs: 1 },
      {
        mkdirSync(path, options) {
          if (path === lock) {
            lockAttempts += 1;
            if (lockAttempts < 3) {
              const error = new Error("lock held");
              error.code = "EEXIST";
              throw error;
            }
          }
          return mkdirSync(path, options);
        },
        now: (() => {
          let value = 0;
          return () => value++;
        })(),
        sleepSync() {
          sleeps += 1;
        },
      },
    );

    assert.equal(lockAttempts, 3);
    assert.equal(sleeps, 2);
    assert.equal(existsSync(join(target, "open-kimi-ppt", "SKILL.md")), true);
    assert.equal(existsSync(lock), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("times out on a held install lock without changing an installation", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const destination = join(target, "open-kimi-ppt");
  const skillFile = join(destination, "SKILL.md");
  const lock = join(target, ".open-kimi-ppt.install.lock");

  try {
    mkdirSync(destination, { recursive: true });
    mkdirSync(lock);
    writeFileSync(skillFile, "previous installation", "utf8");

    assert.throws(
      () => installSkill({ target, lockTimeoutMs: 0 }),
      (error) => error.message.includes(lock),
    );
    assert.equal(readFileSync(skillFile, "utf8"), "previous installation");
    assert.equal(existsSync(lock), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("preserves the stable backup and aggregates install and rollback failures", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const destination = join(target, "open-kimi-ppt");
  const backup = join(target, ".open-kimi-ppt.backup");

  try {
    mkdirSync(destination, { recursive: true });
    writeFileSync(join(destination, "SKILL.md"), "previous installation", "utf8");
    let renameCount = 0;

    assert.throws(
      () => installSkill(
        { target },
        {
          renameSync(source, output) {
            renameCount += 1;
            if (renameCount === 2) throw new Error("injected install failure");
            if (renameCount === 3) throw new Error("injected rollback failure");
            return renameSync(source, output);
          },
        },
      ),
      (error) => {
        assert.equal(error instanceof AggregateError, true);
        assert.match(error.message, /rollback also failed/);
        assert.match(error.message, new RegExp(backup.replaceAll("\\", "\\\\")));
        assert.equal(error.errors.length, 2);
        return true;
      },
    );
    assert.equal(readFileSync(join(backup, "SKILL.md"), "utf8"), "previous installation");
    assert.equal(existsSync(destination), false);
    assert.equal(existsSync(join(target, ".open-kimi-ppt.install.lock")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("refuses to overwrite a preserved backup from an earlier failed install", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const destination = join(target, "open-kimi-ppt");
  const backup = join(target, ".open-kimi-ppt.backup");

  try {
    mkdirSync(destination, { recursive: true });
    mkdirSync(backup);
    writeFileSync(join(destination, "SKILL.md"), "active installation", "utf8");
    writeFileSync(join(backup, "SKILL.md"), "preserved installation", "utf8");

    assert.throws(() => installSkill({ target }), (error) => error.message.includes(backup));
    assert.equal(readFileSync(join(destination, "SKILL.md"), "utf8"), "active installation");
    assert.equal(readFileSync(join(backup, "SKILL.md"), "utf8"), "preserved installation");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("reports and preserves a backup that cannot be removed after replacement", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");
  const destination = join(target, "open-kimi-ppt");
  const backup = join(target, ".open-kimi-ppt.backup");

  try {
    mkdirSync(destination, { recursive: true });
    writeFileSync(join(destination, "SKILL.md"), "previous installation", "utf8");

    assert.throws(
      () => installSkill(
        { target },
        {
          rmSync(path, options) {
            if (path === backup) throw new Error("injected backup cleanup failure");
            return rmSync(path, options);
          },
        },
      ),
      (error) => error.message.includes(backup),
    );
    assert.match(readFileSync(join(destination, "SKILL.md"), "utf8"), /^---\r?\nname: open-kimi-ppt/m);
    assert.equal(readFileSync(join(backup, "SKILL.md"), "utf8"), "previous installation");
    assert.equal(existsSync(join(target, ".open-kimi-ppt.install.lock")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("accepts legacy --force without changing overwrite behavior", () => {
  const root = mkdtempSync(join(tmpdir(), "open-kimi-ppt-test-"));
  const target = join(root, "skills");

  try {
    assert.equal(runCli(["--target", target]).status, 0);
    const result = runCli(["--target", target, "--force"]);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Updated open-kimi-ppt/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
