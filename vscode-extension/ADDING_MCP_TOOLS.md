# 加新 MCP 工具后的完整部署步骤

这是**部署协商**，不是学习。Cursor 不会因为仓库里出现了新函数就自动把工具交给 Agent。换一台机器、换一个项目、只改 Python 不打包，现场都看不到新工具。

历史：`get_recent_pain_events`（0.11.0）→ `record_human_correction`（0.11.1）→ `get_recent_plans`（0.11.2）。每加一个工具都要走完整条链。漏一步会出现「代码在、面板没有、Agent 调不到」——那是工具未加载，不要写成规则未消费。

不改四值枚举，不改 Sidecar 判定。本文件只记部署。

## 0. 改代码时必须同时改的标记

Cursor 扩展会在 activate 时检查 vendored Python 是否含全部 `REQUIRED_SIDECAR_MARKERS`（`vscode-extension/src/mcpConfig.ts`）。缺任何一个，扩展会把 `~/.cursor/mcp.json` 的 `PYTHONPATH` 改回当前包。

加新的 off-lattice 工具时：

1. 在 `audit_sidecar/mcp_server.py` 实现该工具，并挂进 `tools/list`。
2. 把工具名加进 `REQUIRED_SIDECAR_MARKERS`。
3. 确认该工具走 `_handle_tools_call` 短路，**不进** `_execute` / `audit_trace`（off-lattice 约定）。
4. 握手 / `memory_bus` 测试覆盖 `tools/list` 含新名、调用不被 trace。

当前标记（0.11.2）：`get_recent_pain_events`、`record_human_correction`、`get_recent_corrections`、`get_recent_plans`。

## 1. 同步 Python 进扩展包

在 sidecar 仓库根：

```bash
cd vscode-extension
npm run sync-python
# 即 node scripts/sync-python.js
# 把 audit_sidecar 拷进 vendor/harness-audit-sidecar/python
# vendor python 默认 gitignore，不以源码树当现场
```

不要只改仓库里的 `audit_sidecar/` 然后指望已安装的 0.10.9 / 旧版扩展读到它。现场 MCP 的 `PYTHONPATH` 指向扩展目录下的 vendored 副本。

## 2. 升版本并打包

```bash
# vscode-extension/package.json 的 version 加一格（例：0.11.2 → 0.11.3）
npx vsce package --allow-missing-repository
# 产出 vscode-extension/harness-audit-<version>.vsix（gitignore）
```

`verify.mjs` 里不要在注释中留下新工具名字符串，否则 `pythonRootHasRequiredTools` 会误判为已同步。

## 3. 安装到本机 Cursor

```bash
cursor --install-extension vscode-extension/harness-audit-<version>.vsix
# 或 VS Code：code --install-extension <同上>
```

确认扩展目录出现：

`~/.cursor/extensions/liao-zhizhi.harness-audit-<version>/`

旧版本目录可能仍在。以 `mcp.json` 的 `PYTHONPATH` 实际指向哪一版为准。

## 4. Reload Window

`Developer: Reload Window`。扩展 activate 会检查 vendored 树是否含全部 marker：

- 含齐：保留现有 `harness-audit` 条目（含 `--cwd`）。
- 缺 marker：把 `PYTHONPATH` 写成当前包的 vendored python。

Reload 后再读：

```bash
python3 -c "import json; print(json.load(open('$HOME/.cursor/mcp.json'))['mcpServers']['harness-audit'])"
```

`PYTHONPATH` 必须指向**刚装的那一版** `.../harness-audit-<version>/node_modules/harness-audit-sidecar/python`，不能指回 0.10.9 或源码树（除非你明确在做开发绑定，且 marker 已齐）。

## 5. 确认 tools/list（以握手为准）

Cursor MCP 面板可能滞后，**不要以面板为准**。对正在跑的 harness-audit 进程做 `tools/list`（或新开对话看 Agent 工具目录）。必须同时出现：

- 原有：`write_file` / `append_file` / `run_command` / `validate_citation`
- 本轮新工具名

「面板未刷新，不影响调用」——若握手已有、面板没有，不要重装一轮。若握手没有，回到第 1 步，不要先改规则文件。

## 6. 换机器 / 换项目时整条重来

这套状态不跟着 Gradia git 走：

| 状态 | 在哪 |
| --- | --- |
| 工具实现 | sidecar `audit_sidecar/`，需 sync 进 vsix |
| marker | `vscode-extension/src/mcpConfig.ts` |
| 现场 PYTHONPATH | `~/.cursor/mcp.json`（用户级，不在 Gradia 仓库） |
| Cursor cwd | 常为 `$HOME`，不是打开的项目根；off-lattice 靠 `--cwd` 找 `agent_sentience` |
| 规则 | 项目 `.cursor/rules/audit.mdc`（alwaysApply） |
| 事件 / 预案 | `$GRADIA_HOME/memory/*.jsonl` 或环境变量覆盖 |

新机器：clone sidecar → 从第 1 步打包安装。新项目：项目根要有 `agent_sentience/`（或把 `--cwd` 指到有该包的树），并放 `audit.mdc`。只拷 Gradia 源码、不装对应版本 vsix，新工具不会出现。

## 不要做的事

- 不要把「tools/list 没有新工具」写成「规则未触发 / 规则未消费」。
- 不要在未 sync、未打包、未 Reload 的情况下，只改 `mcp.json` 的 PYTHONPATH 到源码树然后 Reload——0.10.9 会写回旧包；0.11.0+ 在 marker 不齐时同样会写回。
- 不要改 `VerdictEngine` / 四值枚举来「让工具出现」。工具出现是部署问题。
