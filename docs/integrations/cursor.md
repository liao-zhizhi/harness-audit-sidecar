---
layout: default
title: Cursor
permalink: /integrations/cursor.html
---

# Cursor

最优路径是装 VSIX，点侧边栏 **一键开启因果审计**。插件会写入用户级 `~/.cursor/mcp.json`，并把 `--cwd` / `--trace` 绑到当前工作区绝对路径。

## One-click

1. 下载 [harness-audit-0.10.9.vsix](https://github.com/liao-zhizhi/harness-audit-sidecar/releases/download/v0.10.9/harness-audit-0.10.9.vsix)
2. `cursor --install-extension harness-audit-0.10.9.vsix`
3. 打开工作区，点盾牌视图里的 **一键开启因果审计**，然后 Reload Window

## Manual `~/.cursor/mcp.json`

先安装 CLI：`pipx install harness-audit-sidecar`（或 `pip install harness-audit-sidecar`）。把 `/ABS/PATH/TO/PROJECT` 换成项目的绝对路径。

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
      "cwd": "/ABS/PATH/TO/PROJECT"
    }
  }
}
```

换工作区时同步改 `--cwd`、`--trace` 和 `cwd`。改完后 Reload Window。
