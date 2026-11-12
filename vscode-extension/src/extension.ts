import * as vscode from "vscode";
import { AuditViewProvider } from "./panel";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new AuditViewProvider(context);

  const bindWorkspace = (): void => {
    provider.syncGlobalWorkspaceBind();
  };

  const bindAndStart = (): void => {
    provider.syncGlobalWorkspaceBind();
    if (provider.shouldAutoStart()) {
      provider.start();
    }
  };

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("harnessAudit.panel", provider),
    vscode.commands.registerCommand("harnessAudit.enable", () => provider.enable()),
    vscode.commands.registerCommand("harnessAudit.start", () => provider.start()),
    vscode.commands.registerCommand("harnessAudit.stop", () => provider.stop()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => bindAndStart()),
    vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) {
        bindWorkspace();
      }
    }),
    { dispose: () => provider.dispose() }
  );

  bindAndStart();
  // A new window's workspaceFolders can be empty on the first tick.
  // onDidChangeWorkspaceFolders does not fire for "open folder in a new window".
  const retries = [100, 400].map((ms) =>
    setTimeout(() => {
      provider.syncGlobalWorkspaceBind();
      if (provider.shouldAutoStart() && !provider.process.running) {
        provider.start();
      }
    }, ms)
  );
  context.subscriptions.push({
    dispose: () => retries.forEach(clearTimeout),
  });
}

export function deactivate(): void {
  // provider.dispose via subscriptions
}
