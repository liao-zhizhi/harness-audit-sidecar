---
layout: default
title: Cline
permalink: /integrations/cline.html
---

# Cline

`harness-audit-server` 走 stdio MCP。先安装：`pipx install harness-audit-sidecar`（或 `pip install harness-audit-sidecar`）。把 `/ABS/PATH/TO/PROJECT` 换成项目的绝对路径。

## UI

1. 打开 Cline 面板，点工具栏的 MCP Servers 图标
2. Configure → **Configure MCP Servers**（会打开 `cline_mcp_settings.json`）
3. 把下面的 `harness-audit` 条目并进 `mcpServers`
4. 保存；必要时 Reload Window

## `cline_mcp_settings.json`

VS Code 扩展通常打开的是该 JSON。Cline CLI 则是 `~/.cline/data/settings/cline_mcp_settings.json`。

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
      ],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

`command` 必须在 PATH 里。找不到时改成 `harness-audit-server` 的绝对路径（`which harness-audit-server`）。换项目时同步改 `--cwd` 和 `--trace`。
