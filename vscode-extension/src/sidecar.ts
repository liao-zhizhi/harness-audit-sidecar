/**
 * Locate and spawn the Python sidecar without pipx.
 *
 * Resolution order:
 * 1. ``require("harness-audit-sidecar").pythonRoot()`` (npm file: dependency)
 * 2. ``harness-audit-server`` on PATH
 * 3. ``python3 -m audit_sidecar.mcp_server`` with PYTHONPATH
 *
 * Git is not required to *start* the process. The Python witness still lives
 * in ``audit_sidecar/git_witness.py`` (full tier) and
 * ``SafeRunner._stat_snapshot`` in ``audit_sidecar/runner.py`` (declared-file
 * mtime/size only, no tree walk). Non-git workspaces currently record
 * INVALID on the full tier — that is the Python gate, not this connector.
 *
 * ``--cwd`` and ``--trace`` always come from the active workspace root, never
 * from the editor process cwd.
 */

import { ChildProcess, spawn, spawnSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { resolveWorkspaceRoot, workspaceBindArgs } from "./mcpConfig";

export interface Runtime {
  python: string;
  pythonRoot: string;
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
}

function locatorRoot(): string | undefined {
  try {
    const loc = require("harness-audit-sidecar") as { pythonRoot: () => string };
    const root = loc.pythonRoot();
    if (fs.existsSync(path.join(root, "audit_sidecar", "__init__.py"))) {
      return root;
    }
  } catch {
    // not installed yet
  }
  return undefined;
}

export function resolvePythonRoot(extensionPath: string): string {
  const fromNpm = locatorRoot();
  if (fromNpm) {
    return fromNpm;
  }
  const parent = path.resolve(extensionPath, "..");
  if (fs.existsSync(path.join(parent, "audit_sidecar", "__init__.py"))) {
    return parent;
  }
  const vendored = path.join(
    extensionPath,
    "vendor",
    "harness-audit-sidecar",
    "python"
  );
  return vendored;
}

function pythonHasSidecar(python: string, pythonRoot: string): boolean {
  const probe = spawnSync(
    python,
    ["-c", "import audit_sidecar, yaml; print(audit_sidecar.__version__)"],
    {
      env: { ...process.env, PYTHONPATH: pythonRoot },
      encoding: "utf8",
    }
  );
  return probe.status === 0;
}

function which(name: string): string | undefined {
  const probe = spawnSync("which", [name], { encoding: "utf8" });
  const found = (probe.stdout || "").trim();
  return probe.status === 0 && found ? found : undefined;
}

export function resolveRuntime(extensionPath: string): Runtime {
  const pythonRoot = resolvePythonRoot(extensionPath);
  const python = which("python3") || "python3";
  if (pythonHasSidecar(python, pythonRoot)) {
    return {
      python,
      pythonRoot,
      command: python,
      args: ["-m", "audit_sidecar.mcp_server"],
      env: { ...process.env, PYTHONPATH: pythonRoot },
    };
  }
  const onPath = which("harness-audit-server");
  if (onPath) {
    return {
      python,
      pythonRoot,
      command: onPath,
      args: [],
      env: { ...process.env, PYTHONPATH: pythonRoot },
    };
  }
  throw new Error(
    "找不到 Sidecar：python3 无法 import audit_sidecar。把插件放在 harness-audit-sidecar 仓库里，或把包拷进 vendor/harness-audit-sidecar/python。"
  );
}

export class SidecarProcess {
  private child: ChildProcess | undefined;
  running = false;

  start(runtime: Runtime, workspaceRoot: string): void {
    this.stop();
    const abs = resolveWorkspaceRoot(workspaceRoot);
    const args = [...runtime.args, ...workspaceBindArgs(abs)];
    this.child = spawn(runtime.command, args, {
      cwd: abs,
      env: runtime.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    // Do not write to stdin. This process speaks JSON-RPC; an empty write is
    // at best a no-op and at worst a torn first frame if a newline follows.
    this.child.on("exit", () => {
      this.running = false;
      this.child = undefined;
    });
    this.running = true;
  }

  stop(): void {
    if (this.child && !this.child.killed) {
      this.child.kill();
    }
    this.child = undefined;
    this.running = false;
  }
}
