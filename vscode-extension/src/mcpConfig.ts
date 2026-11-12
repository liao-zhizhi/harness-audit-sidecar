/**
 * Merge a harness-audit MCP server into ``.cursor/mcp.json``.
 *
 * Same contract as ``audit_sidecar.setup_cmd.merge_mcp``: other servers stay,
 * our entry is upserted, the file is never replaced wholesale.
 */

import * as fs from "fs";
import * as path from "path";

export const SERVER_NAME = "harness-audit";
export const MCP_RELPATH = path.join(".cursor", "mcp.json");

export interface McpServerEntry {
  command: string;
  args: string[];
  env?: Record<string, string>;
}

export function mcpPathFor(workspaceRoot: string): string {
  return path.join(workspaceRoot, MCP_RELPATH);
}

export function isEnabled(workspaceRoot: string): boolean {
  const file = mcpPathFor(workspaceRoot);
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
  workspaceRoot: string,
  entry: McpServerEntry
): { changed: boolean; path: string } {
  const file = mcpPathFor(workspaceRoot);
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

export function buildSidecarEntry(
  python: string,
  pythonRoot: string,
  workspaceRoot: string
): McpServerEntry {
  return {
    command: python,
    args: ["-m", "audit_sidecar.mcp_server", "--cwd", workspaceRoot],
    env: { PYTHONPATH: pythonRoot },
  };
}
