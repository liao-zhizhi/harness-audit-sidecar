import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import {
  buildSidecarEntry,
  injectGlobalMcpJson,
  isEnabled,
  isEnabledInWorkspace,
  readGlobalSidecarEntry,
  resolveWorkspaceRoot,
  shouldPreserveExistingSidecar,
  TRACE_NAME,
} from "./mcpConfig";
import { Runtime, SidecarProcess, resolveRuntime } from "./sidecar";
import { loadTrace, renderPanelHtml } from "./trace";

export class AuditViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private runtime: Runtime | undefined;
  private watcher: fs.FSWatcher | undefined;
  private refreshTimer: NodeJS.Timeout | undefined;
  readonly process = new SidecarProcess();

  constructor(private readonly context: vscode.ExtensionContext) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.onDidReceiveMessage((message) => {
      if (message?.type === "enable") {
        void this.enable();
      } else if (message?.type === "start") {
        this.start();
      } else if (message?.type === "stop") {
        this.stop();
      }
    });
    this.render();
    this.watchTrace();
  }

  workspaceRoot(): string | undefined {
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    return folder ? resolveWorkspaceRoot(folder) : undefined;
  }

  globallyEnabled(): boolean {
    return isEnabled();
  }

  shouldAutoStart(): boolean {
    const root = this.workspaceRoot();
    return this.globallyEnabled() || (root ? isEnabledInWorkspace(root) : false);
  }

  private ensureRuntime(): Runtime {
    if (!this.runtime) {
      this.runtime = resolveRuntime(this.context.extensionPath);
    }
    return this.runtime;
  }

  /**
   * Bind ``~/.cursor/mcp.json`` only when the harness-audit server is missing
   * or its Python tree is stale (missing an off-lattice tool). A complete
   * existing entry is left alone so Reload cannot wipe a newer local package.
   */
  syncGlobalWorkspaceBind(): boolean {
    const root = this.workspaceRoot();
    if (!root) {
      return false;
    }
    if (shouldPreserveExistingSidecar(readGlobalSidecarEntry())) {
      return false;
    }
    if (!this.globallyEnabled()) {
      return false;
    }
    try {
      const runtime = this.ensureRuntime();
      const { changed } = this.bindGlobalSidecar(runtime, root, false);
      if (changed) {
        this.promptReload(root);
      }
      return changed;
    } catch {
      return false;
    }
  }

  /** Write ~/.cursor/mcp.json. ``force`` is for the Enable button only. */
  private bindGlobalSidecar(
    runtime: Runtime,
    workspaceRoot: string,
    force = false
  ): {
    changed: boolean;
    path: string;
  } {
    return injectGlobalMcpJson(
      buildSidecarEntry(runtime.python, runtime.pythonRoot, workspaceRoot),
      os.homedir(),
      { force }
    );
  }

  private lastPromptedBind: string | undefined;

  private promptReload(root: string): void {
    if (this.lastPromptedBind === root) {
      return;
    }
    this.lastPromptedBind = root;
    void vscode.window
      .showInformationMessage(
        `审计工作区已绑定到 ${root}。请重启窗口，让 Cursor 的 MCP 进程使用新的 --cwd / --trace。`,
        "重启窗口"
      )
      .then((choice) => {
        if (choice === "重启窗口") {
          return vscode.commands.executeCommand("workbench.action.reloadWindow");
        }
        return undefined;
      });
  }

  async enable(): Promise<void> {
    const root = this.workspaceRoot();
    if (!root) {
      const message =
        "请先打开一个工作区文件夹。全局审计器必须把 --cwd / --trace 绑到 vscode.workspace.workspaceFolders[0]。";
      void vscode.window.showErrorMessage(message);
      this.render(message);
      return;
    }
    let runtime: Runtime;
    try {
      runtime = this.ensureRuntime();
    } catch (error) {
      void vscode.window.showErrorMessage(String(error));
      this.render(String(error));
      return;
    }
    const result = this.bindGlobalSidecar(runtime, root, true);
    this.start();
    this.render();
    this.lastPromptedBind = root;
    const restart = await vscode.window.showInformationMessage(
      `已写入全局 ${result.path}，--cwd / --trace 已绑定到 ${root}。请重启窗口让 Cursor 加载 MCP。`,
      "重启窗口"
    );
    if (restart === "重启窗口") {
      await vscode.commands.executeCommand("workbench.action.reloadWindow");
    }
  }

  start(): void {
    const root = this.workspaceRoot();
    if (!root) {
      this.render();
      return;
    }
    try {
      const runtime = this.ensureRuntime();
      this.syncGlobalWorkspaceBind();
      // Cursor owns the stdio MCP process from ~/.cursor/mcp.json. Spawning a
      // second mcp_server here races that client and leaves the agent on
      // "Not connected" after pkill / reload. Only spawn when MCP is not
      // globally configured (legacy local panel).
      if (!this.globallyEnabled()) {
        this.process.start(runtime, root);
      }
      this.watchTrace();
      this.render();
    } catch (error) {
      this.render(String(error));
    }
  }

  stop(): void {
    this.process.stop();
    this.render();
  }

  dispose(): void {
    this.process.stop();
    this.watcher?.close();
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
    }
  }

  private tracePath(): string | undefined {
    const root = this.workspaceRoot();
    return root ? path.join(root, TRACE_NAME) : undefined;
  }

  private watchTrace(): void {
    this.watcher?.close();
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
    }
    const file = this.tracePath();
    const root = this.workspaceRoot();
    if (!file || !root) {
      return;
    }
    try {
      this.watcher = fs.watch(root, (_event, name) => {
        if (!name || name === TRACE_NAME || String(name).endsWith(TRACE_NAME)) {
          this.render();
        }
      });
    } catch {
      // empty folders on some FS cannot watch yet
    }
    this.refreshTimer = setInterval(() => {
      if (this.process.running) {
        this.render();
      }
    }, 1500);
  }

  private panelBody(): string {
    const file = this.tracePath();
    if (!file || !fs.existsSync(file)) {
      return renderPanelHtml([]);
    }
    try {
      return renderPanelHtml(loadTrace(fs.readFileSync(file, "utf8")));
    } catch (error) {
      return `<p class="empty">无法读取 audit_trace.jsonl：${String(error)}</p>`;
    }
  }

  private render(error?: string): void {
    if (!this.view) {
      return;
    }
    const enabled = this.globallyEnabled();
    // When globally enabled, Cursor's MCP process is the sidecar. The panel
    // child is not spawned, so process.running would stay false (Stop) even
    // though mcp.json is bound — that was the shield showing Stop after pkill.
    const running = this.process.running || enabled;
    const nonce = String(Date.now());
    const body = enabled
      ? this.panelBody()
      : `<button class="go" id="enable">一键开启因果审计</button>
         <p class="hint">一次写入用户级 <code>~/.cursor/mcp.json</code>，并把 <code>--cwd</code> / <code>--trace</code> 绑到当前工作区根目录。日志落在该文件夹的 <code>audit_trace.jsonl</code>。不改仓库、不要求 git、不要求 pipx。</p>`;
    this.view.webview.html = `<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font: 13px/1.45 var(--vscode-font-family); color: var(--vscode-foreground); margin: 0; padding: 12px; }
  .status { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--vscode-widget-border); }
  .on { background: #238636; color: #fff; border-color: #238636; }
  .off { opacity: 0.7; }
  button { cursor: pointer; border-radius: 6px; border: 1px solid var(--vscode-button-border, transparent); padding: 6px 10px; }
  .go { display: block; width: 100%; background: #238636; color: #fff; font-size: 15px; font-weight: 600; padding: 14px 12px; border: 0; border-radius: 8px; }
  .hint { opacity: 0.8; font-size: 12px; }
  .row { border-top: 1px solid var(--vscode-widget-border); padding: 8px 0; }
  .head { font-family: var(--vscode-editor-font-family); }
  .pass { color: #3fb950; font-weight: 700; }
  .fail { color: #f85149; font-weight: 700; }
  .invalid { color: #d29922; font-weight: 700; }
  .notrun { color: #8b949e; font-weight: 700; }
  .fail-hl { color: #f85149; font-weight: 600; }
  .stats { opacity: 0.85; }
  .empty { opacity: 0.8; }
  .err { color: #f85149; }
  h2 { font-size: 13px; margin: 0 0 8px; }
</style>
</head>
<body>
  <div class="status">
    <span class="pill ${running ? "on" : "off"}">${running ? "Start · 运行中" : "Stop · 已停止"}</span>
    ${!enabled && this.process.running ? `<button id="stop">Stop</button>` : ""}
    ${enabled ? `<span class="hint">Cursor 管 MCP。若 Agent 报 Not connected，请重启窗口。</span>` : ""}
  </div>
  ${error ? `<p class="err">${String(error).replace(/[<>&]/g, "")}</p>` : ""}
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.getElementById("enable")?.addEventListener("click", () => vscode.postMessage({ type: "enable" }));
    document.getElementById("start")?.addEventListener("click", () => vscode.postMessage({ type: "start" }));
    document.getElementById("stop")?.addEventListener("click", () => vscode.postMessage({ type: "stop" }));
  </script>
</body>
</html>`;
  }
}
