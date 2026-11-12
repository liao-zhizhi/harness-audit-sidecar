import * as vscode from "vscode";
import { AuditViewProvider } from "./panel";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new AuditViewProvider(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("harnessAudit.panel", provider),
    vscode.commands.registerCommand("harnessAudit.enable", () => provider.enable()),
    vscode.commands.registerCommand("harnessAudit.start", () => provider.start()),
    vscode.commands.registerCommand("harnessAudit.stop", () => provider.stop()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      if (provider.shouldAutoStart()) {
        provider.start();
      }
    }),
    { dispose: () => provider.dispose() }
  );

  if (provider.shouldAutoStart()) {
    provider.start();
  }
}

export function deactivate(): void {
  // provider.dispose via subscriptions
}
