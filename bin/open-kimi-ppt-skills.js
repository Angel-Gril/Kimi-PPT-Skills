#!/usr/bin/env node

import { cpSync, existsSync, mkdirSync, mkdtempSync, renameSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { startEditorServer } from "../lib/editor-server.js";

const SKILL_NAME = "open-kimi-ppt";
const MIN_NODE_MAJOR = 18;
const LOCK_TIMEOUT_MS = 30_000;
const LOCK_RETRY_MS = 50;
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceDirectory = join(packageRoot, "skills", SKILL_NAME);

function assertNodeVersion() {
  const major = Number.parseInt(process.versions.node.split(".")[0], 10);
  if (!Number.isInteger(major) || major < MIN_NODE_MAJOR) {
    throw new Error(
      `Node.js ${MIN_NODE_MAJOR}+ is required; found ${process.version}. Install from https://nodejs.org`,
    );
  }
}

function printHelp(command) {
  if (command === "serve") {
    console.log(`Start the local PPTD editor and exporter.

Usage:
  open-kimi-ppt-skills serve [options]

Options:
  --port <number>  HTTP port (default: 55173)
  --open           Open the editor in the default browser
  -h, --help       Show this help
`);
    return;
  }

  console.log(`Install ${SKILL_NAME} for your AI coding agent or start its local editor.

Usage:
  open-kimi-ppt-skills [install] [options]
  open-kimi-ppt-skills serve [options]

Install options:
  --target <directory>  Skills directory (default: ~/.agents/skills)

Re-running install replaces an existing open-kimi-ppt installation.
Run "open-kimi-ppt-skills serve --help" for server options.
`);
}

function parseArguments(arguments_) {
  const args = [...arguments_];
  const command = args[0] === "install" || args[0] === "serve" ? args.shift() : "install";
  const options = command === "serve"
    ? { command, open: false, port: 55173 }
    : { command, target: undefined };

  while (args.length > 0) {
    const argument = args.shift();

    // Accepted for backward compatibility; install always overwrites.
    if (command === "install" && argument === "--force") {
      continue;
    }

    if (command === "install" && argument === "--target") {
      const target = args.shift();
      if (!target || target.startsWith("-")) {
        throw new Error("--target requires a directory");
      }
      options.target = resolve(target);
      continue;
    }

    if (command === "serve" && argument === "--port") {
      const port = Number(args.shift());
      if (!Number.isInteger(port) || port < 1 || port > 65_535) {
        throw new Error("--port must be an integer between 1 and 65535");
      }
      options.port = port;
      continue;
    }

    if (command === "serve" && argument === "--open") {
      options.open = true;
      continue;
    }

    if (argument === "--help" || argument === "-h") {
      options.help = true;
      continue;
    }

    throw new Error(`unknown argument: ${argument}`);
  }

  return options;
}

function openBrowser(url) {
  const command = process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  const child = spawn(command, args, { detached: true, stdio: "ignore" });
  child.on("error", (error) => console.warn(`Could not open the browser: ${error.message}`));
  child.unref();
}

function defaultSkillsDirectory() {
  return join(homedir(), ".agents", "skills");
}

const requiredRuntimeFiles = [
  "SKILL.md",
  "requirements.txt",
  join("scripts", "export_pptx.py"),
  join("scripts", "export_images.py"),
  join("scripts", "browser_export.py"),
  join("scripts", "export_host.html"),
];

const defaultFileOperations = {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  now: Date.now,
  sleepSync(milliseconds) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
  },
};

function assertPackagedSkillComplete(root, exists = existsSync) {
  const missing = requiredRuntimeFiles.filter((relative) => !exists(join(root, relative)));
  if (missing.length > 0) {
    throw new Error(`packaged skill is incomplete; missing: ${missing.join(", ")}`);
  }
}

function combineErrors(primary, secondary, message) {
  return new AggregateError([primary, secondary], message);
}

function acquireInstallLock(lockPath, operations, timeoutMilliseconds, retryMilliseconds) {
  const deadline = operations.now() + timeoutMilliseconds;

  while (true) {
    try {
      operations.mkdirSync(lockPath);
      return;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      if (operations.now() >= deadline) {
        throw new Error(
          `timed out waiting for another ${SKILL_NAME} install; lock remains at ${lockPath}`,
          { cause: error },
        );
      }
      operations.sleepSync(retryMilliseconds);
    }
  }
}

function installTransaction({ skillsDirectory, destination, backup, operations }) {
  const {
    cpSync: copy,
    existsSync: exists,
    mkdtempSync: makeTemporaryDirectory,
    renameSync: rename,
    rmSync: remove,
  } = operations;

  if (exists(backup)) {
    throw new Error(
      `cannot install because a previous installation backup already exists at ${backup}; `
      + "preserve or restore it before retrying",
    );
  }

  const replaced = exists(destination);
  const stagingRoot = makeTemporaryDirectory(join(skillsDirectory, `.${SKILL_NAME}-staging-`));
  const stagedSkill = join(stagingRoot, SKILL_NAME);
  let transactionError;

  try {
    copy(sourceDirectory, stagedSkill, {
      recursive: true,
      filter: (source) => ![".DS_Store", "_user_meta.json"].includes(basename(source)),
    });
    assertPackagedSkillComplete(stagedSkill, exists);

    let oldMoved = false;
    if (replaced) {
      rename(destination, backup);
      oldMoved = true;
    }

    try {
      rename(stagedSkill, destination);
    } catch (installError) {
      if (oldMoved) {
        try {
          if (!exists(backup)) {
            throw new Error(`previous installation backup is missing from ${backup}`);
          }
          if (exists(destination)) {
            throw new Error(`cannot restore backup because the destination is occupied: ${destination}`);
          }
          rename(backup, destination);
          oldMoved = false;
        } catch (restoreError) {
          throw combineErrors(
            installError,
            restoreError,
            `skill update failed and rollback also failed; the previous installation is preserved at ${backup}`,
          );
        }
      }
      throw installError;
    }

    if (oldMoved) {
      try {
        remove(backup, { recursive: true });
      } catch (cleanupError) {
        throw new Error(
          `skill was updated, but the previous installation backup could not be removed; `
          + `it is preserved at ${backup}`,
          { cause: cleanupError },
        );
      }
    }
  } catch (error) {
    transactionError = error;
  }

  try {
    remove(stagingRoot, { recursive: true, force: true });
  } catch (cleanupError) {
    transactionError = transactionError
      ? combineErrors(
          transactionError,
          cleanupError,
          `${transactionError.message}; staging cleanup also failed at ${stagingRoot}`,
        )
      : new Error(`installed skill, but staging cleanup failed at ${stagingRoot}`, {
          cause: cleanupError,
        });
  }

  if (transactionError) throw transactionError;
  return replaced;
}

function installSkill(
  { target, lockTimeoutMs = LOCK_TIMEOUT_MS, lockRetryMs = LOCK_RETRY_MS },
  injectedFileOperations = {},
) {
  const operations = { ...defaultFileOperations, ...injectedFileOperations };
  assertPackagedSkillComplete(sourceDirectory, operations.existsSync);

  const skillsDirectory = target ?? defaultSkillsDirectory();
  const destination = join(skillsDirectory, SKILL_NAME);
  const backup = join(skillsDirectory, `.${SKILL_NAME}.backup`);
  const lock = join(skillsDirectory, `.${SKILL_NAME}.install.lock`);

  operations.mkdirSync(skillsDirectory, { recursive: true });
  acquireInstallLock(lock, operations, lockTimeoutMs, lockRetryMs);

  let replaced;
  let installError;
  try {
    replaced = installTransaction({
      skillsDirectory,
      destination,
      backup,
      operations,
    });
  } catch (error) {
    installError = error;
  }

  try {
    operations.rmSync(lock, { recursive: true });
  } catch (releaseError) {
    installError = installError
      ? combineErrors(
          installError,
          releaseError,
          `${installError.message}; install lock could not be released at ${lock}`,
        )
      : new Error(`skill was installed, but its lock could not be released at ${lock}`, {
          cause: releaseError,
        });
  }

  if (installError) throw installError;

  console.log(
    replaced
      ? `Updated ${SKILL_NAME} at ${destination}`
      : `Installed ${SKILL_NAME} to ${destination}`,
  );
}

async function main() {
  assertNodeVersion();
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    printHelp(options.command);
    return;
  }

  if (options.command === "install") {
    installSkill(options);
    return;
  }

  const { server, url } = await startEditorServer({ port: options.port });
  console.log(`Open Kimi PPT editor is running at ${url}`);
  console.log("Press Ctrl+C to stop the server.");
  if (options.open) openBrowser(url);

  const shutdown = () => server.close(() => process.exit(0));
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

export { installSkill };

const invokedAsScript = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedAsScript) {
  main().catch((error) => {
    console.error(`Error: ${error.message}`);
    process.exitCode = 1;
  });
}
