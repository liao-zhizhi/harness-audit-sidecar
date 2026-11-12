---
layout: default
title: Claude Code
permalink: /integrations/claude-code.html
---

# Claude Code

`harness-audit-server` 走 stdio MCP。先安装：`pipx install harness-audit-sidecar`（或 `pip install harness-audit-sidecar`）。把 `/ABS/PATH/TO/PROJECT` 换成项目的绝对路径。

## `claude mcp add`

`--` 后面才是服务器参数，否则 Claude 会把 `--cwd` 当成自己的 flag。

```bash
claude mcp add --transport stdio --scope user harness-audit -- \
  harness-audit-server \
  --cwd /ABS/PATH/TO/PROJECT \
  --trace /ABS/PATH/TO/PROJECT/audit_trace.jsonl
```

只给当前仓库用时，把 `--scope user` 改成 `--scope local`（默认），或 `--scope project` 写入仓库根的 `.mcp.json`。

```bash
claude mcp list
```

## JSON 片段

`claude mcp add --scope user` 把条目写进 `~/.claude.json`。手改时用同一段；若你维护的是 `~/.claude/settings.json`，把 `mcpServers` 块原样放进去：

```json
{
  "mcpServers": {
    "harness-audit": {
      "type": "stdio",
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

换项目时改两个绝对路径。审计日志落在该项目的 `audit_trace.jsonl`。

遇到问题？请提交 Issue：https://github.com/liao-zhizhi/harness-audit-sidecar/issues
