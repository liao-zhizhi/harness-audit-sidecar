# Trial kit (10 people)

A one-week trial. You do not change product code. You keep working as usual. Once a week you count the false completions this auditor caught that you would have missed.

## Install

Pick one:

```bash
pipx install 'harness-audit-sidecar>=0.11.3'
```

Or pack and install the Cursor/VS Code extension from this repo (current package version is `0.11.4`):

```bash
cursor --install-extension vscode-extension/harness-audit-0.11.4.vsix
```

After install, **Reload once** so the editor picks up the MCP server. Without Reload, `tools/list` can still show the old process.

## Verify

1. Ask the agent to run `echo test > trial_check.txt` in the project root (create the file in an existing directory). Then open `audit_trace.jsonl` in the working directory and look for a `PASS` on that write.
2. Ask the agent to write a file under a directory that does not exist (do not create the parent first). Then check `audit_trace.jsonl` for a `FAIL`.

If step 1 has no `PASS`, Reload and confirm the sidecar is the process attached to this window. If step 2 has no `FAIL`, the write may have created the parent first; try again with an explicit “do not mkdir” instruction.

## One-week protocol

- Do not change your codebase for this trial.
- Use the auditor only during normal work.
- Once at the end of the week, record how many false completions it caught that you would have ignored. Use [FEEDBACK_TEMPLATE.md](FEEDBACK_TEMPLATE.md). One row per incident is enough; a week with zero incidents is a valid result.

## How to fill the feedback template

Copy [FEEDBACK_TEMPLATE.md](FEEDBACK_TEMPLATE.md). One row:

- Date
- Project type: Web / script / data / other
- What the agent claimed
- Sidecar verdict: `PASS` / `FAIL` / `INVALID` / `NOT_RUN`
- Did that verdict change your next action: yes / no / unsure
- One-line note

Do not paste secrets, credentials, or large file contents. A path prefix and the verdict are enough.

## Scope

This trial only covers witnesses for file writes and command execution. Cross-session rule distillation and complex permission cases are not verified here. A `NOT_RUN` or `INVALID` means the auditor declined to judge, not that the agent succeeded.
