# 示例：同一会话里的四条审计记录

`sample_trace.jsonl` 不是手写的，是用下面这条命令真实跑出来后原样保存的。
它演示了整个项目的核心主张：**Agent 收到的答复与审计账本可以是相反的，而且两者都对各自的
读者正确**。

## 协议层看到的东西（Agent 收到的）

```
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", "isError": false, ...}}
{"jsonrpc": "2.0", "id": 2, "result": {"status": "ok", "isError": false, ...}}   <- 伪装
{"jsonrpc": "2.0", "id": 3, "result": {"status": "error", "isError": true, ...}}
{"jsonrpc": "2.0", "id": 4, "result": {"status": "ok", "isError": false, ...}}   <- 伪装
```

## 审计账本看到的东西（`audit_trace.jsonl`）

| id | 调用 | agent 声明 | 真实效果 | 判词 | 说明 |
|--:|:--|:--|:--|:--|:--|
| 1 | `write_file target.py` | 成功 | 已写入 | **PASS** | 诚实调用 |
| 2 | `write_file missing_dir/target.py` | 成功 | 无变化 | **FAIL** | 工具层吞掉路径错误 |
| 3 | `write_file target.py` | 失败 | 已写入 | **FAIL** | 假失败：写成了却报错 |
| 4 | `run_command`（期望改 `target.py`） | 成功 | 改的是 `wrong.py` | **FAIL** | 目标漂移 |

**四条调用里有三条 FAIL，而 Agent 只被明确告知一次失败（id=3）。**
另外两次，Agent 收到的是 `ok`。

## 重点看 id=2

```json
{
  "id": 2,
  "tool": "write_file",
  "check_observed": true,
  "check_source": "protocol",
  "effect_observed": false,
  "verdict": "FAIL",
  "reason": "false completion: report says success, witness says no effect (expected but untouched: missing_dir/target.py) [tool layer reported success, underlying command exited 1]",
  "missing_expected": ["missing_dir/target.py"],
  "exit_code": 1,
  "pipe_status": [1]
}
```

三个字段值得对照着看：

- `check_observed: true` —— 工具层对 Agent 说的话。
- `effect_observed: false` —— git 与文件系统看到的实情。
- `exit_code: 1` —— 命令自己的退出码。工具层看到了 1，然后换成了 `ok`。

`reason` 里那句 `[tool layer reported success, underlying command exited 1]`
同时留下了**两套观测**。如果 `check` 当初是从 `exit_code` 推导出来的，
这条记录会变成 `check: false`，判词变成"意图未实现"——**判词凑巧一样，但被告换了人**：
本该怀疑工具层，结果怀疑了 Agent。

## 为什么不脱敏

这个文件里**没有任何绝对路径**（可用下面的命令复核，结果为 0）：

```bash
grep -cE '/tmp/|/home/' examples/sample_trace.jsonl
```

这不是运气，是字段设计所致：

| 字段 | 路径形态 |
|:--|:--|
| `changed_files` | 仓库根相对（`git status` 的坐标系） |
| `missing_expected` | 仓库根相对（经 `resolve_expected` 映射） |
| `unexpected_changes` | 仓库根相对 |
| `intent` | 回显客户端给的 `args.path` **原文** |

**唯一的例外是 `intent`**：它原样回显调用参数。上面这份样例的客户端传的是相对路径
（`target.py`），所以整份文件干净。若你的客户端传绝对路径，`intent` 里就会出现本机路径
（形如 `<TEMP_DIR>/target.py`）——这不是脱敏遗漏，而是"记录被审计的声明"所必付的代价。
分享 trace 前请用上面那条 `grep` 自查。

> 这条边界已记入 `README.md` 的「已知边界」。

## 如何重新生成

在任意临时 git 仓库里跑（`--cwd` 指向该仓库）：

```bash
D=$(mktemp -d) && cd "$D"
git init -q && : > target.py && : > other.py
git add -A && git -c user.name=T -c user.email=t@e commit -qm init

printf '%s\n' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"target.py","content":"value = 1\n"},"reported_success":true},"id":1}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"missing_dir/target.py","content":"value = 2\n"},"reported_success":true},"id":2}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"target.py","content":"value = 3\n"},"reported_success":false},"id":3}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"run_command","args":{"command":"printf \"x\" > wrong.py","expected_files_modified":["target.py"]},"reported_success":true},"id":4}' \
 | PYTHONPATH=/path/to/agent-harness-audit python3 -m audit_sidecar.mcp_server --cwd "$D" --quiet

cat "$D/audit_trace.jsonl"
```

id=3 那条要靠 `reported_success:false` 构造，因为真实工具层不会主动谎报失败——
这一条是为了覆盖判词的另一侧（假失败），不是真实故障的复现。

## 文件格式

一行一条记录，字段按字母序（`json.dumps(..., sort_keys=True)`），无缩进。
这样追加写入时单次 `write` 不会撕裂记录，`tail -f` 和日志采集器都能安全并行读取。
