import * as vscode from "vscode";
import { AuditViewProvider } from "./panel";
import { isEnabled } from "./mcpConfig";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new AuditViewProvider(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("harnessAudit.panel", provider),
    vscode.commands.registerCommand("harnessAudit.enable", () => provider.enable()),
    vscode.commands.registerCommand("harnessAudit.start", () => provider.start()),
    vscode.commands.registerCommand("harnessAudit.stop", () => provider.stop()),
    { dispose: () => provider.dispose() }
  );

  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (root && isEnabled(root)) {
    provider.start();
  }
}

export function deactivate(): void {
  // provider.dispose via subscriptions
}
