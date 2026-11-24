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
import {
  L0_EVENTS_REL,
  PENDING_CAP,
  formatL0EventLine,
  loadTrace,
  parseL0Events,
  pendingFails,
  renderPanelHtml,
  renderPendingRow,
  renderProcessedRow,
  type L0Event,
  type TraceRow,
} from "./trace";

export class AuditViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private runtime: Runtime | undefined;
  private watcher: fs.FSWatcher | undefined;
  private refreshTimer: NodeJS.Timeout | undefined;
  private painted = false;
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
      } else if (message?.type === "judge") {
        void this.judge(String(message.timestamp || ""), String(message.path || ""), String(message.button || ""));
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
    if (!this.globallyEnabled()) {
      return false;
    }
    try {
      const runtime = this.ensureRuntime();
      // Pass the Python root this extension would write now. A vendored copy
      // left behind by an earlier version lives in a sibling extensions
      // directory and must not be preserved, or Reload keeps running old code.
      if (shouldPreserveExistingSidecar(readGlobalSidecarEntry(), runtime.pythonRoot)) {
        return false;
      }
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
        const label = String(name || "");
        if (!name || label === TRACE_NAME || label.endsWith(TRACE_NAME) || label.endsWith("l0_events.md")) {
          this.pushQueue();
        }
      });
    } catch {
      // empty folders on some FS cannot watch yet
    }
    this.refreshTimer = setInterval(() => {
      if (this.process.running || this.globallyEnabled()) {
        this.pushQueue();
      }
    }, 1500);
  }

  private eventsPath(): string | undefined {
    const root = this.workspaceRoot();
    return root ? path.join(root, L0_EVENTS_REL) : undefined;
  }

  private loadRows(): TraceRow[] {
    const file = this.tracePath();
    if (!file || !fs.existsSync(file)) {
      return [];
    }
    return loadTrace(fs.readFileSync(file, "utf8"));
  }

  private loadEvents(): L0Event[] {
    const file = this.eventsPath();
    if (!file || !fs.existsSync(file)) {
      return [];
    }
    return parseL0Events(fs.readFileSync(file, "utf8"));
  }

  private queuePayload(): {
    pendingFixedHtml: string;
    pendingOverflowHtml: string;
    processedHtml: string;
    history: string;
    extra: number;
    pendingCount: number;
  } {
    const rows = this.loadRows();
    const events = this.loadEvents();
    const pending = pendingFails(rows, events);
    const processed = events.slice().reverse();
    return {
      pendingFixedHtml: pending.slice(0, PENDING_CAP).map(renderPendingRow).join(""),
      pendingOverflowHtml: pending.slice(PENDING_CAP).map(renderPendingRow).join(""),
      processedHtml: processed.map(renderProcessedRow).join(""),
      history: renderPanelHtml(rows),
      extra: Math.max(0, pending.length - PENDING_CAP),
      pendingCount: pending.length,
    };
  }

  private judge(timestamp: string, filePath: string, button: string): void {
    const allowed = new Set(["重做", "跳过", "不确定"]);
    if (!timestamp || !filePath || !allowed.has(button)) {
      return;
    }
    const file = this.eventsPath();
    const root = this.workspaceRoot();
    if (!file || !root) {
      return;
    }
    const already = this.loadEvents().some(
      (item) => item.timestamp === timestamp && item.path === filePath
    );
    if (already) {
      this.pushQueue();
      return;
    }
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const line = formatL0EventLine(timestamp, filePath, button);
    const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
    const pad = existing && !existing.endsWith("\n") ? "\n" : "";
    fs.appendFileSync(file, `${pad}${line}\n`, "utf8");
    this.pushQueue();
  }

  private pushQueue(): void {
    if (!this.view || !this.globallyEnabled()) {
      return;
    }
    if (!this.painted) {
      this.render();
      return;
    }
    void this.view.webview.postMessage({ type: "queue", ...this.queuePayload() });
  }

  private panelBody(): string {
    try {
      const payload = this.queuePayload();
      return `
        <section id="pending-block">
          <h2 class="pending-title">待判断</h2>
          <div id="pending-fixed">${payload.pendingFixedHtml}</div>
          <button type="button" id="pending-expand" data-extra="${payload.extra}"${payload.extra > 0 ? "" : " hidden"}>还有 ${payload.extra} 条，点击展开</button>
          <div id="pending-overflow" hidden>${payload.pendingOverflowHtml}</div>
          <p id="pending-empty" class="empty"${payload.pendingCount ? " hidden" : ""}>没有待判断的 FAIL 或 INVALID。</p>
        </section>
        <section id="processed-block">
          <details>
            <summary>已处理</summary>
            <div id="processed-list">${payload.processedHtml || `<p class="empty">还没有点过按钮。</p>`}</div>
          </details>
        </section>
        <section id="history-block">
          ${payload.history}
        </section>`;
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
    this.painted = true;
    this.view.webview.html = `<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  html, body { height: 100%; }
  body { font: 13px/1.45 var(--vscode-font-family); color: var(--vscode-foreground); margin: 0; display: flex; flex-direction: column; min-height: 100%; overflow: hidden; }
  .status { display: flex; align-items: center; gap: 8px; padding: 8px 12px; flex: 0 0 auto; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--vscode-widget-border); }
  .on { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border-color: var(--vscode-button-background); }
  .off { opacity: 0.7; }
  button { cursor: pointer; border-radius: 6px; border: 1px solid var(--vscode-button-border, var(--vscode-widget-border)); padding: 6px 10px; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  .go { display: block; width: 100%; background: var(--vscode-button-background); color: var(--vscode-button-foreground); font-size: 15px; font-weight: 600; padding: 14px 12px; border: 0; border-radius: 8px; }
  .hint { opacity: 0.8; font-size: 12px; }
  .row { border-top: 1px solid var(--vscode-widget-border); padding: 8px 0; }
  .head { font-family: var(--vscode-editor-font-family); }
  .pass { color: var(--vscode-testing-iconPassed, var(--vscode-foreground)); font-weight: 700; }
  .fail { color: var(--vscode-errorForeground); font-weight: 700; }
  .invalid { color: var(--vscode-editorWarning-foreground, var(--vscode-foreground)); font-weight: 700; }
  .notrun { opacity: 0.7; font-weight: 700; }
  .fail-hl { color: var(--vscode-errorForeground); font-weight: 600; }
  .stats { opacity: 0.85; }
  .empty { opacity: 0.8; }
  .err { color: var(--vscode-errorForeground); padding: 0 12px; }
  h2 { font-size: 13px; margin: 0 0 8px; }
  #pending-block { flex: 0 0 auto; border-left: 3px solid var(--vscode-errorForeground); padding: 8px 12px; }
  .pending-title { color: var(--vscode-errorForeground); }
  .pending-row, .processed-row { display: flex; align-items: center; gap: 6px; white-space: nowrap; min-height: 28px; min-width: 0; width: 100%; box-sizing: border-box; overflow: hidden; }
  .pending-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
  .pending-dot-fail { background: var(--vscode-errorForeground); }
  .pending-dot-invalid { background: var(--vscode-editorWarning-foreground, var(--vscode-charts-yellow, #d29922)); }
  .pending-mark { flex: 0 0 auto; font-size: 10px; font-weight: 700; }
  .pending-mark-fail { color: var(--vscode-errorForeground); }
  .pending-mark-invalid { color: var(--vscode-editorWarning-foreground, var(--vscode-charts-yellow, #d29922)); }
  .pending-name { overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1 1 0; }
  .pending-time { flex: 0 0 auto; opacity: 0.8; }
  .pending-btns { flex: 0 0 auto; display: flex; gap: 2px; }
  .pending-btns button { padding: 2px 5px; font-size: 11px; }
  #pending-overflow { max-height: 8em; overflow: auto; }
  #pending-expand { width: 100%; margin-top: 4px; font-size: 12px; }
  #processed-block { flex: 0 0 auto; padding: 4px 12px 8px; }
  .processed-btn { flex: 0 0 auto; opacity: 0.85; }
  #history-block { flex: 1 1 auto; overflow: auto; padding: 8px 12px 12px; }
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
    let expanded = false;
    document.getElementById("enable")?.addEventListener("click", () => vscode.postMessage({ type: "enable" }));
    document.getElementById("start")?.addEventListener("click", () => vscode.postMessage({ type: "start" }));
    document.getElementById("stop")?.addEventListener("click", () => vscode.postMessage({ type: "stop" }));
    function bindPending(root) {
      root?.querySelectorAll(".pending-row button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const row = btn.closest(".pending-row");
          vscode.postMessage({
            type: "judge",
            timestamp: row?.dataset.ts,
            path: row?.dataset.path,
            button: btn.getAttribute("data-btn"),
          });
        });
      });
    }
    function applyQueue(data) {
      const fixed = document.getElementById("pending-fixed");
      const overflow = document.getElementById("pending-overflow");
      const expand = document.getElementById("pending-expand");
      const empty = document.getElementById("pending-empty");
      const processedList = document.getElementById("processed-list");
      const history = document.getElementById("history-block");
      if (fixed && data.pendingFixedHtml !== undefined) fixed.innerHTML = data.pendingFixedHtml;
      if (overflow && data.pendingOverflowHtml !== undefined) overflow.innerHTML = data.pendingOverflowHtml;
      if (expand) {
        expand.hidden = !(data.extra > 0);
        expand.dataset.extra = String(data.extra || 0);
        expand.textContent = expanded ? "收起" : ("还有 " + (data.extra || 0) + " 条，点击展开");
      }
      if (overflow) overflow.hidden = !expanded || !(data.extra > 0);
      if (empty) empty.hidden = data.pendingCount > 0;
      if (processedList) {
        processedList.innerHTML = data.processedHtml || '<p class="empty">还没有点过按钮。</p>';
      }
      if (history && data.history) history.innerHTML = data.history;
      bindPending(fixed);
      bindPending(overflow);
    }
    document.getElementById("pending-expand")?.addEventListener("click", () => {
      expanded = !expanded;
      const overflow = document.getElementById("pending-overflow");
      const expand = document.getElementById("pending-expand");
      if (overflow) overflow.hidden = !expanded;
      if (expand) {
        const extra = expand.dataset.extra || "0";
        expand.textContent = expanded ? "收起" : ("还有 " + extra + " 条，点击展开");
      }
    });
    bindPending(document.getElementById("pending-fixed"));
    bindPending(document.getElementById("pending-overflow"));
    window.addEventListener("message", (event) => {
      if (event.data?.type === "queue") applyQueue(event.data);
    });
  </script>
</body>
</html>`;
  }
}
