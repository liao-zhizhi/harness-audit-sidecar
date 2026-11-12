"use strict";

const fs = require("fs");
const path = require("path");

const src = path.resolve(__dirname, "..", "..", "audit_sidecar");
const destParent = path.resolve(
  __dirname,
  "..",
  "vendor",
  "harness-audit-sidecar",
  "python"
);

if (!fs.existsSync(path.join(src, "__init__.py"))) {
  process.stderr.write("sync-python: audit_sidecar not next to vscode-extension; skip\n");
  process.exit(0);
}

function copyPackage(destParent) {
  const dest = path.join(destParent, "audit_sidecar");
  fs.mkdirSync(destParent, { recursive: true });
  fs.cpSync(src, dest, {
    recursive: true,
    filter: (name) => !name.includes("__pycache__") && !name.endsWith(".pyc"),
  });
}

copyPackage(destParent);
const bundled = path.resolve(
  __dirname,
  "..",
  "node_modules",
  "harness-audit-sidecar",
  "python"
);
if (fs.existsSync(path.dirname(bundled))) {
  copyPackage(bundled);
}
process.stdout.write("sync-python: copied audit_sidecar -> vendor/harness-audit-sidecar/python\n");
