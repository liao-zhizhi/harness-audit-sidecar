# For developers

Catches false completion in coding agents.

This is the developer view of **harness-audit-sidecar**. Same package, same four-value verdicts, same install. It audits the claim, not the trace.

## What it does / 它做什么

When an agent says it finished, sidecar audits that claim with independent physical witnesses: file hashes, mtime, test exit codes, git diff. It emits `PASS` / `FAIL` / `INVALID` / `NOT_RUN`.

Agent 说「完成了」时，用这些见证核对声称，而不是给会话打分。

## What it does not do / 它不做什么

- Not an observability platform. No traces, eval scores, or dashboards. 不是可观测性平台。
- Not an MCP security scanner. It does not hunt tool poisoning or config holes. 不是 MCP 安全扫描器。
- Not a code-review or test-generation tool. It does not judge code quality. It only judges whether “I completed it” matches the physical effect. 不是代码审查或测试生成工具。

## Install / 安装

```bash
pipx install 'harness-audit-sidecar'
```

Current pin is in the root README (and on PyPI). Do not copy a version number here.

Or install the VSIX from GitHub Releases / the site download button. Cursor steps: [integrations/cursor.md](integrations/cursor.md). After install, **Reload once**.

## One-week trial / 一周试用

Do not change your codebase for this trial. Use sidecar during normal work. At the end of the week, count false completions it caught that you would have ignored. Zero incidents is a valid result.

Verify once:

1. Ask the agent to run `echo test > trial_check.txt` in an existing directory. Check `audit_trace.jsonl` for `PASS`.
2. Ask the agent to write a file under a directory that does not exist (do not mkdir first). Check `audit_trace.jsonl` for `FAIL`.

## Feedback / 反馈

One row per incident. Do not paste secrets.

| Date | Project type (Web / script / data / other) | What the agent claimed | Sidecar verdict (PASS / FAIL / INVALID / NOT_RUN) | Did this verdict change your next action? (yes / no / unsure) | One-line note |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Scope / 边界

Current coverage is file-write and command-execution witnesses. Complex permission cases and cross-session rule distillation are not verified here. `NOT_RUN` or `INVALID` means sidecar declined to judge, not that the agent succeeded.
