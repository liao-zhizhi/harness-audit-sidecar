/**
 * Merge a harness-audit MCP server into the **user-level** Cursor config.
 *
 * One write to ``~/.cursor/mcp.json`` covers every workspace. The entry has
 * no baked-in ``--cwd``: Cursor starts the server with the open folder as
 * cwd, so ``audit_trace.jsonl`` lands in that folder. Other servers in the
 * file are preserved.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export const SERVER_NAME = "harness-audit";
export const MCP_RELPATH = path.join(".cursor", "mcp.json");

export interface McpServerEntry {
  command: string;
  args: string[];
  env?: Record<string, string>;
}

export function globalMcpPath(homeDir: string = os.homedir()): string {
  return path.join(homeDir, MCP_RELPATH);
}

export function mcpPathFor(workspaceRoot: string): string {
  return path.join(workspaceRoot, MCP_RELPATH);
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
  pythonRoot: string
): McpServerEntry {
  return {
    command: python,
    args: ["-m", "audit_sidecar.mcp_server"],
    env: { PYTHONPATH: pythonRoot },
  };
}
