#!/usr/bin/env node
"use strict";

import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createRequire } from "module";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { injectMcpJson, isEnabled, buildSidecarEntry, mcpPathFor } = require("../out/mcpConfig.js");
const { loadTrace, renderPanelHtml, countVerdicts } = require("../out/trace.js");
const { resolvePythonRoot, resolveRuntime } = require("../out/sidecar.js");

const extRoot = join(here, "..");
const empty = mkdtempSync(join(tmpdir(), "harness-audit-empty-"));
let failed = 0;

function check(name, cond) {
  if (!cond) {
    failed += 1;
    console.error("FAIL", name);
  } else {
    console.log("ok  ", name);
  }
}

try {
  const pythonRoot = resolvePythonRoot(extRoot);
  check("pythonRoot has audit_sidecar", require("fs").existsSync(join(pythonRoot, "audit_sidecar", "__init__.py")));

  const runtime = resolveRuntime(extRoot);
  const entry = buildSidecarEntry(runtime.python, runtime.pythonRoot, empty);
  check("not enabled before inject", !isEnabled(empty));
  const written = injectMcpJson(empty, entry);
  check("mcp.json path is .cursor/mcp.json", written.path === mcpPathFor(empty));
  check("enabled after inject", isEnabled(empty));
  const doc = JSON.parse(readFileSync(written.path, "utf8"));
  check("server name harness-audit", Boolean(doc.mcpServers["harness-audit"]));
  check("no pipx in command", !String(doc.mcpServers["harness-audit"].command).includes("pipx"));
  check("module is mcp_server", doc.mcpServers["harness-audit"].args.includes("audit_sidecar.mcp_server"));
  check("cwd is the empty workspace", doc.mcpServers["harness-audit"].args.includes(empty));

  const sample = join(extRoot, "..", "examples", "sample_trace.jsonl");
  const rows = loadTrace(readFileSync(sample, "utf8"));
  const html = renderPanelHtml(rows);
  const counts = countVerdicts(rows);
  check("panel has PASS badge", html.includes("【PASS】"));
  check("panel has FAIL badge", html.includes("【FAIL】"));
  check("panel highlights missing files", html.includes("没动的文件"));
  check("counts include FAIL", counts.FAIL >= 1);
  check("counts include PASS", counts.PASS >= 1);

  const workspace = join(extRoot, ".test-workspace");
  mkdirSync(workspace, { recursive: true });
  writeFileSync(join(workspace, "audit_trace.jsonl"), readFileSync(sample));
  check("F5 workspace starts without mcp.json", !isEnabled(workspace));
  check("F5 workspace has jsonl for the panel", require("fs").existsSync(join(workspace, "audit_trace.jsonl")));

  console.log("\nworkspace", empty);
  console.log(readFileSync(written.path, "utf8"));
} finally {
  rmSync(empty, { recursive: true, force: true });
}

if (failed) {
  process.exit(1);
}
console.log("\nverify: all checks passed");
