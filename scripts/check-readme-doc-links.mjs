#!/usr/bin/env node
// Fail if README.md links a soctalk-docs page that no longer exists.
//
// The README hand-curates a subset of the docs nav, so renamed or deleted
// pages drift silently. This clones the docs repo, reads its ALL_PAGES export
// (the single source of truth for the sidebar), and asserts every
// soctalk-docs URL in README.md resolves to a real page.
//
// Network-tolerant: if the docs repo can't be cloned (offline CI, outage) it
// SKIPS with a warning rather than failing the build. It only exits non-zero
// on a genuinely unknown link.
//
// Usage: node scripts/check-readme-doc-links.mjs

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const BASE = "https://soctalk.github.io/soctalk-docs";
const DOCS_REPO = "https://github.com/soctalk/soctalk-docs.git";

const readme = readFileSync(new URL("../README.md", import.meta.url), "utf8");
const links = [
  ...new Set(
    [...readme.matchAll(/https:\/\/soctalk\.github\.io\/soctalk-docs\/?[^\s)\]]*/g)]
      .map((m) => m[0].replace(/[.,]$/, "").replace(/\/$/, "").replace(/#.*$/, "")),
  ),
].filter((u) => u !== BASE); // the bare docs-home link is always valid

if (links.length === 0) {
  console.log("check-readme-doc-links: no docs links to check.");
  process.exit(0);
}

const dir = mkdtempSync(join(tmpdir(), "soctalk-docs-"));
try {
  try {
    execFileSync("git", ["clone", "--depth=1", "--quiet", DOCS_REPO, dir], {
      stdio: ["ignore", "ignore", "inherit"],
    });
  } catch (e) {
    console.warn(
      `check-readme-doc-links: SKIP (could not clone docs repo: ${e.message}).`,
    );
    process.exit(0);
  }

  const mod = await import(
    pathToFileURL(join(dir, "docs/.vitepress/i18n/structure.mjs")).href
  );
  const allPages = mod.ALL_PAGES;
  if (!Array.isArray(allPages)) {
    console.warn("check-readme-doc-links: SKIP (ALL_PAGES export not found).");
    process.exit(0);
  }

  // ALL_PAGES holds slug strings ('quickstart-vm', 'guides/existing-wazuh').
  const valid = new Set(
    allPages.map((slug) => `${BASE}/${String(slug).replace(/^\/+/, "").replace(/\/$/, "")}`),
  );

  const missing = links.filter((u) => !valid.has(u));
  if (missing.length) {
    console.error(
      "check-readme-doc-links: README links pages not present in the docs nav:\n" +
        missing.map((u) => `  ${u}`).join("\n") +
        "\n\nUpdate README.md or the docs, then re-run.",
    );
    process.exit(1);
  }
  console.log(`check-readme-doc-links: OK (${links.length} docs links resolve).`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
