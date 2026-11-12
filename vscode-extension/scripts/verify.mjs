#!/usr/bin/env node
"use strict";

import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createRequire } from "module";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const {
  injectGlobalMcpJson,
  isEnabled,
  isEnabledInWorkspace,
  buildSidecarEntry,
  globalMcpPath,
} = require("../out/mcpConfig.js");
const { loadTrace, renderPanelHtml, countVerdicts } = require("../out/trace.js");
const { resolvePythonRoot, resolveRuntime } = require("../out/sidecar.js");

const extRoot = join(here, "..");
const fakeHome = mkdtempSync(join(tmpdir(), "harness-audit-home-"));
const emptyWs = mkdtempSync(join(tmpdir(), "harness-audit-ws-"));
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
  const entry = buildSidecarEntry(runtime.python, runtime.pythonRoot);
  check("global entry has no baked-in workspace cwd", !entry.args.includes(emptyWs));
  check("global entry is the python module", entry.args.includes("audit_sidecar.mcp_server"));
  check("not globally enabled before inject", !isEnabled(fakeHome));
  check("workspace is not required", !isEnabledInWorkspace(emptyWs));

  const written = injectGlobalMcpJson(entry, fakeHome);
  check("writes ~/.cursor/mcp.json under the user home", written.path === globalMcpPath(fakeHome));
  check("does not create project .cursor/mcp.json", !isEnabledInWorkspace(emptyWs));
  check("globally enabled after inject", isEnabled(fakeHome));

  const doc = JSON.parse(readFileSync(written.path, "utf8"));
  const server = doc.mcpServers["harness-audit"];
  check("server name harness-audit", Boolean(server));
  check("no pipx in command", !String(server.command).includes("pipx"));
  check("module is mcp_server", server.args.includes("audit_sidecar.mcp_server"));
  check("no --cwd in the global entry", !server.args.includes("--cwd"));

  const otherHome = mkdtempSync(join(tmpdir(), "harness-audit-home2-"));
  mkdirSync(join(otherHome, ".cursor"), { recursive: true });
  writeFileSync(
    join(otherHome, ".cursor", "mcp.json"),
    JSON.stringify({ mcpServers: { other: { command: "echo" } } }, null, 2)
  );
  injectGlobalMcpJson(entry, otherHome);
  const merged = JSON.parse(readFileSync(join(otherHome, ".cursor", "mcp.json"), "utf8"));
  check("preserves other global servers", Boolean(merged.mcpServers.other));
  check("upserts harness-audit beside them", Boolean(merged.mcpServers["harness-audit"]));
  rmSync(otherHome, { recursive: true, force: true });

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
  check("a workspace folder is not how enablement is stored", !isEnabledInWorkspace(workspace));

  console.log("\nglobal mcp.json");
  console.log(readFileSync(written.path, "utf8"));
} finally {
  rmSync(fakeHome, { recursive: true, force: true });
  rmSync(emptyWs, { recursive: true, force: true });
}

if (failed) {
  process.exit(1);
}
console.log("\nverify: all checks passed");
