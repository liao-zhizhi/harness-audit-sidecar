---
layout: default
title: Windsurf
permalink: /integrations/windsurf.html
---

# Windsurf

`harness-audit-server` 走 stdio MCP。先安装：`pipx install harness-audit-sidecar`（或 `pip install harness-audit-sidecar`）。把 `/ABS/PATH/TO/PROJECT` 换成项目的绝对路径。

## Cascade UI

1. Windsurf **Settings → Cascade → MCP Servers**
2. Add Server / 打开 raw `mcp_config.json`
3. 并入下面的 `harness-audit` 条目
4. 保存后在 MCP 面板点 Refresh（或完全重启 Windsurf）

## JSON 片段

Cascade 打开的配置一般是 `~/.codeium/windsurf/mcp_config.json`（Linux/macOS）或 `%USERPROFILE%\.codeium\windsurf\mcp_config.json`（Windows）。

```json
{
  "mcpServers": {
    "harness-audit": {
      "command": "harness-audit-server",
      "args": [
        "--cwd",
        "/ABS/PATH/TO/PROJECT",
        "--trace",
        "/ABS/PATH/TO/PROJECT/audit_trace.jsonl"
      ]
    }
  }
}
```

`command` 必须在 PATH 里。找不到时改成绝对路径。换项目时同步改 `--cwd` 和 `--trace`。审计日志落在该项目的 `audit_trace.jsonl`。
