/**
 * Merge a harness-audit MCP server into the **user-level** Cursor config.
 *
 * One write to ``~/.cursor/mcp.json`` covers every workspace. Cursor does
 * **not** start a user-level MCP server with the open folder as cwd (it
 * inherits the editor process cwd, typically ``$HOME``). Opening a different
 * folder in a new window also does **not** fire ``onDidChangeWorkspaceFolders``
 * in the previous window, and Cursor may spawn the MCP process from a stale
 * mcp.json before the extension activates.
 *
 * So every enable / activate / start / folder change / focused-window sync
 * must force-write ``--cwd`` and ``--trace`` (and the spawn ``cwd`` field)
 * to the absolute path of ``workspaceFolders[0]``.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export const SERVER_NAME = "harness-audit";
export const MCP_RELPATH = path.join(".cursor", "mcp.json");
export const TRACE_NAME = "audit_trace.jsonl";

export interface McpServerEntry {
  command: string;
  args: string[];
  env?: Record<string, string>;
  /** Cursor spawn directory. Sidecar ``--cwd`` is the source of truth. */
  cwd?: string;
}

export function globalMcpPath(homeDir: string = os.homedir()): string {
  return path.join(homeDir, MCP_RELPATH);
}

export function mcpPathFor(workspaceRoot: string): string {
  return path.join(workspaceRoot, MCP_RELPATH);
}

/** Absolute workspace root. Relative paths would bind the sidecar to $HOME. */
export function resolveWorkspaceRoot(workspaceRoot: string): string {
  if (!workspaceRoot) {
    throw new Error("workspaceRoot is required to bind --cwd and --trace");
  }
  return path.resolve(workspaceRoot);
}

export function tracePathFor(workspaceRoot: string): string {
  return path.join(resolveWorkspaceRoot(workspaceRoot), TRACE_NAME);
}

/** ``--cwd`` / ``--trace`` flags for the sidecar, pinned to one workspace. */
export function workspaceBindArgs(workspaceRoot: string): string[] {
  const abs = resolveWorkspaceRoot(workspaceRoot);
  return ["--cwd", abs, "--trace", tracePathFor(abs)];
}

export function flagValue(args: string[] | undefined, flag: string): string | undefined {
  if (!args) {
    return undefined;
  }
  const index = args.indexOf(flag);
  if (index < 0 || index + 1 >= args.length) {
    return undefined;
  }
  return args[index + 1];
}

export function boundCwdFromEntry(entry: {
  args?: string[];
  cwd?: string;
} | undefined): string | undefined {
  if (!entry) {
    return undefined;
  }
  const fromFlag = flagValue(entry.args, "--cwd");
  if (fromFlag) {
    return path.resolve(fromFlag);
  }
  return entry.cwd ? path.resolve(entry.cwd) : undefined;
}

export function workspaceBindMatches(
  entry: McpServerEntry | undefined,
  workspaceRoot: string
): boolean {
  if (!entry) {
    return false;
  }
  const abs = resolveWorkspaceRoot(workspaceRoot);
  const cwd = boundCwdFromEntry(entry);
  const trace = flagValue(entry.args, "--trace");
  return (
    cwd === abs &&
    entry.cwd === abs &&
    trace === tracePathFor(abs)
  );
}

function hasServer(file: string): boolean {
  if (!fs.existsSync(file)) {
    return false;
  }
  try {
    const doc = JSON.parse(fs.readFileSync(file, "utf8")) as {
      mcpServers?: Record<string, unknown>;
    };
    return Boolean(doc.mcpServers && doc.mcpServers[SERVER_NAME]);
  } catch {
    return false;
  }
}

export function isEnabled(homeDir: string = os.homedir()): boolean {
  return hasServer(globalMcpPath(homeDir));
}

export function isEnabledInWorkspace(workspaceRoot: string): boolean {
  return hasServer(mcpPathFor(workspaceRoot));
}

export function readGlobalSidecarEntry(
  homeDir: string = os.homedir()
): McpServerEntry | undefined {
  const file = globalMcpPath(homeDir);
  if (!fs.existsSync(file)) {
    return undefined;
  }
  try {
    const doc = JSON.parse(fs.readFileSync(file, "utf8")) as {
      mcpServers?: Record<string, McpServerEntry>;
    };
    return doc.mcpServers?.[SERVER_NAME];
  } catch {
    return undefined;
  }
}

export function mergeMcpDocument(
  document: unknown,
  entry: McpServerEntry
): { changed: boolean; merged: Record<string, unknown> } {
  let merged: Record<string, unknown>;
  if (document === null || document === undefined || document === "") {
    merged = {};
  } else if (typeof document !== "object" || Array.isArray(document)) {
    throw new Error("the existing config is not a JSON object");
  } else {
    merged = { ...(document as Record<string, unknown>) };
  }

  const rawServers = merged.mcpServers;
  let servers: Record<string, unknown>;
  if (rawServers === null || rawServers === undefined) {
    servers = {};
  } else if (typeof rawServers !== "object" || Array.isArray(rawServers)) {
    throw new Error("mcpServers is not a JSON object");
  } else {
    servers = { ...(rawServers as Record<string, unknown>) };
  }

  const current = servers[SERVER_NAME];
  if (current && JSON.stringify(current) === JSON.stringify(entry)) {
    return { changed: false, merged };
  }
  servers[SERVER_NAME] = entry;
  merged.mcpServers = servers;
  return { changed: true, merged };
}

export function injectMcpJson(
  file: string,
  entry: McpServerEntry
): { changed: boolean; path: string } {
  let document: unknown = {};
  if (fs.existsSync(file)) {
    const text = fs.readFileSync(file, "utf8").trim();
    document = text ? JSON.parse(text) : {};
  }
  const { changed, merged } = mergeMcpDocument(document, entry);
  if (changed) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = file + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(merged, null, 2) + "\n", "utf8");
    fs.renameSync(tmp, file);
  }
  return { changed, path: file };
}

export function injectGlobalMcpJson(
  entry: McpServerEntry,
  homeDir: string = os.homedir()
): { changed: boolean; path: string } {
  return injectMcpJson(globalMcpPath(homeDir), entry);
}

export function buildSidecarEntry(
  python: string,
  pythonRoot: string,
  workspaceRoot: string
): McpServerEntry {
  const abs = resolveWorkspaceRoot(workspaceRoot);
  return {
    command: python,
    args: ["-m", "audit_sidecar.mcp_server", ...workspaceBindArgs(abs)],
    env: { PYTHONPATH: pythonRoot },
    cwd: abs,
  };
}
