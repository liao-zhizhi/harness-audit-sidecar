"use strict";

const fs = require("fs");
const path = require("path");

function pythonRoot() {
  const vendored = path.join(__dirname, "python");
  if (fs.existsSync(path.join(vendored, "audit_sidecar", "__init__.py"))) {
    return vendored;
  }
  // file: install lives at vscode-extension/node_modules/harness-audit-sidecar
  // or vscode-extension/vendor/harness-audit-sidecar; the Python package is
  // the parent of vscode-extension when developing in this repo.
  const extensionRoot = path.resolve(__dirname, "..", "..");
  const repoRoot = path.resolve(extensionRoot, "..");
  if (fs.existsSync(path.join(repoRoot, "audit_sidecar", "__init__.py"))) {
    return repoRoot;
  }
  return vendored;
}

module.exports = {
  pythonRoot,
  version: require("./package.json").version,
};
