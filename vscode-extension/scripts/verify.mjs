#!/usr/bin/env node
"use strict";

import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "fs";
import { spawnSync } from "child_process";
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
  boundCwdFromEntry,
  globalMcpPath,
  pythonRootHasPainTool,
  resolveWorkspaceRoot,
  shouldPreserveExistingSidecar,
  workspaceBindArgs,
  workspaceBindMatches,
  tracePathFor,
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
  const compiled = spawnSync(
    "python3",
    ["-m", "py_compile", join(pythonRoot, "audit_sidecar", "mcp_server.py")],
    { encoding: "utf8" },
  );
  check("mcp_server.py compiles", compiled.status === 0);
  if (compiled.status !== 0) {
    console.error(compiled.stderr || compiled.stdout);
  }

  const runtime = resolveRuntime(extRoot);
  let threw = false;
  try {
    buildSidecarEntry(runtime.python, runtime.pythonRoot, "");
  } catch {
    threw = true;
  }
  check("buildSidecarEntry requires a workspace root", threw);

  const entry = buildSidecarEntry(runtime.python, runtime.pythonRoot, emptyWs);
  const bind = workspaceBindArgs(emptyWs);
  check("global entry includes --cwd", entry.args.includes("--cwd"));
  check("global entry --cwd is the workspace root", entry.args[entry.args.indexOf("--cwd") + 1] === emptyWs);
  check("global entry spawn cwd is the workspace root", entry.cwd === emptyWs);
  check("global entry includes --trace", entry.args.includes("--trace"));
  check(
    "global entry --trace is workspace audit_trace.jsonl",
    entry.args[entry.args.indexOf("--trace") + 1] === tracePathFor(emptyWs)
  );
  check("fresh entry matches the workspace bind", workspaceBindMatches(entry, emptyWs));

  const relative = buildSidecarEntry(runtime.python, runtime.pythonRoot, ".");
  check("relative workspace --cwd is absolute", require("path").isAbsolute(relative.args[relative.args.indexOf("--cwd") + 1]));
  check("relative workspace spawn cwd is absolute", require("path").isAbsolute(relative.cwd));
  check(
    "relative workspace resolves to process cwd",
    relative.cwd === resolveWorkspaceRoot(".") && relative.args[relative.args.indexOf("--cwd") + 1] === resolveWorkspaceRoot(".")
  );
  check("workspaceBindArgs order is --cwd then --trace", bind[0] === "--cwd" && bind[2] === "--trace");
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
  check("mcp.json has --cwd", server.args.includes("--cwd"));
  check("mcp.json --cwd is the active workspace", server.args[server.args.indexOf("--cwd") + 1] === emptyWs);
  check("mcp.json spawn cwd is the active workspace", server.cwd === emptyWs);
  check("mcp.json has --trace", server.args.includes("--trace"));
  check(
    "mcp.json --trace is <workspace>/audit_trace.jsonl",
    server.args[server.args.indexOf("--trace") + 1] === join(emptyWs, "audit_trace.jsonl")
  );
  check("boundCwdFromEntry reads --cwd", boundCwdFromEntry(server) === emptyWs);

  check("synced python tree has required off-lattice tools", pythonRootHasPainTool(pythonRoot));
  check("current runtime python is preservable", shouldPreserveExistingSidecar(entry));
  check("missing entry is not preserved", !shouldPreserveExistingSidecar(undefined));

  const staleHome = mkdtempSync(join(tmpdir(), "harness-audit-home-stale-"));
  const stalePython = mkdtempSync(join(tmpdir(), "harness-audit-old-py-"));
  mkdirSync(join(stalePython, "audit_sidecar"), { recursive: true });
  writeFileSync(
    join(stalePython, "audit_sidecar", "mcp_server.py"),
    "# old sidecar 0.11.2, no tactical upsert\nget_recent_pain_events = True\nrecord_human_correction = True\nget_recent_corrections = True\nget_recent_plans = True\n"
  );
  const staleEntry = {
    command: runtime.python,
    args: ["-m", "audit_sidecar.mcp_server", "--cwd", emptyWs],
    env: { PYTHONPATH: stalePython },
  };
  check(
    "0.11.2 vendored python without _upsert_tactical is not preserved",
    !shouldPreserveExistingSidecar(staleEntry)
  );
  injectGlobalMcpJson(staleEntry, staleHome, { force: true });
  const upgraded = injectGlobalMcpJson(
    buildSidecarEntry(runtime.python, runtime.pythonRoot, emptyWs),
    staleHome
  );
  check("upgrades a stale PYTHONPATH that lacks _upsert_tactical", upgraded.changed);
  const upgradedDoc = JSON.parse(readFileSync(join(staleHome, ".cursor", "mcp.json"), "utf8"));
  const upgradedServer = upgradedDoc.mcpServers["harness-audit"];
  check(
    "upgrade writes absolute --cwd",
    upgradedServer.args[upgradedServer.args.indexOf("--cwd") + 1] === emptyWs
  );
  check(
    "upgrade writes absolute --trace",
    upgradedServer.args[upgradedServer.args.indexOf("--trace") + 1] === join(emptyWs, "audit_trace.jsonl")
  );
  check("upgrade writes spawn cwd", upgradedServer.cwd === emptyWs);
  rmSync(staleHome, { recursive: true, force: true });
  rmSync(stalePython, { recursive: true, force: true });

  const priorHome = mkdtempSync(join(tmpdir(), "harness-audit-home-prior-"));
  const priorPython = mkdtempSync(join(tmpdir(), "harness-audit-prior-py-"));
  mkdirSync(join(priorPython, "audit_sidecar"), { recursive: true });
  writeFileSync(
    join(priorPython, "audit_sidecar", "mcp_server.py"),
    "# old sidecar 0.11.3\nget_recent_pain_events = True\nrecord_human_correction = True\nget_recent_corrections = True\nget_recent_plans = True\n_upsert_tactical = True\n"
  );
  const priorEntry = {
    command: runtime.python,
    args: ["-m", "audit_sidecar.mcp_server", "--cwd", emptyWs],
    env: { PYTHONPATH: priorPython },
  };
  check(
    "0.11.3 vendored python without the newest marker is not preserved",
    !shouldPreserveExistingSidecar(priorEntry)
  );
  injectGlobalMcpJson(priorEntry, priorHome, { force: true });
  const priorUpgraded = injectGlobalMcpJson(
    buildSidecarEntry(runtime.python, runtime.pythonRoot, emptyWs),
    priorHome
  );
  check(
    "upgrades a 0.11.3 PYTHONPATH that lacks the newest marker",
    priorUpgraded.changed
  );
  rmSync(priorHome, { recursive: true, force: true });
  rmSync(priorPython, { recursive: true, force: true });

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

  const otherWs = mkdtempSync(join(tmpdir(), "harness-audit-ws2-"));
  const rebound = buildSidecarEntry(runtime.python, runtime.pythonRoot, otherWs);
  const skipped = injectGlobalMcpJson(rebound, fakeHome);
  check("activate does not rewrite a usable existing sidecar", !skipped.changed);
  const still = JSON.parse(readFileSync(written.path, "utf8")).mcpServers["harness-audit"];
  check(
    "preserved --cwd stays the original workspace",
    still.args[still.args.indexOf("--cwd") + 1] === emptyWs
  );
  const forced = injectGlobalMcpJson(rebound, fakeHome, { force: true });
  check("Enable force-writes a new workspace bind", forced.changed);
  const reboundDoc = JSON.parse(readFileSync(written.path, "utf8"));
  const reboundServer = reboundDoc.mcpServers["harness-audit"];
  check(
    "force rebind updates --cwd",
    reboundServer.args[reboundServer.args.indexOf("--cwd") + 1] === otherWs
  );
  check("force rebind updates spawn cwd", reboundServer.cwd === otherWs);
  check(
    "force rebind updates --trace",
    reboundServer.args[reboundServer.args.indexOf("--trace") + 1] === join(otherWs, "audit_trace.jsonl")
  );
  const same = injectGlobalMcpJson(rebound, fakeHome);
  check("second activate is a no-op once the python tree has the pain tool", !same.changed);
  rmSync(otherWs, { recursive: true, force: true });

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
