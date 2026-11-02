# 示例：同一会话里的审计记录（含一次炎症风暴）

`sample_trace.jsonl` 不是手写的，是用下面这条命令真实跑出来后原样保存的。
它演示了整个项目的核心主张：**Agent 收到的答复与审计账本可以是相反的，而且两者都对各自的
读者正确**。

文件里有两个会话块，字段只增不改：

| 范围 | 生成版本 | 字段 |
|:--|:--|:--|
| id=1..4 | v0.6.1 | `rule_id` / `rules_matched` / `rules_violated` |
| id=5..7 | v0.7.0 | 以上 + `tier` / `witness_scope` / `witness_mtime` / `immune_state` |

前半演示判词，后半演示**条件触发的免疫响应**（见本文末「炎症风暴」一节）。

## 协议层看到的东西（Agent 收到的）

```
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", "isError": false, ...}}
{"jsonrpc": "2.0", "id": 2, "result": {"status": "ok", "isError": false, ...}}   <- 伪装
{"jsonrpc": "2.0", "id": 3, "result": {"status": "error", "isError": true, ...}}
{"jsonrpc": "2.0", "id": 4, "result": {"status": "ok", "isError": false, ...}}   <- 伪装
```

## 审计账本看到的东西（`audit_trace.jsonl`）

| id | 调用 | 字段含义 | 判词 | 说明 |
|--:|:--|:--|:--|:--|
| 1 | `write_file target.py` | ✅ 成功 / ✅ 已写入 | **PASS** | 诚实调用，规则满足 |
| 2 | `write_file missing_dir/target.py` | ✅ 成功 / ❌ 无变化 | **FAIL** | 工具层吞掉路径错误，违反 `no_file_drift` |
| 3 | `write_file target.py` | ❌ 失败 / ✅ 已写入 | **FAIL** | 假失败：写成了却报错 |
| 4 | `run_command`（期望改 `target.py`） | ✅ 成功 / ❌ 改的是 `wrong.py` | **FAIL** | 目标漂移 |

**四条调用里有三条 FAIL，而 Agent 只被明确告知一次失败（id=3）。**
另外两次，Agent 收到的是 `ok`。

## 新增字段：`rule_id`

这份样例是用判据库（`rules/`）跑出来的，所以每条记录多了三个字段：

```json
{
  "rule_id": "no_file_drift",
  "rules_matched": ["no_file_drift"],
  "rules_violated": ["no_file_drift"]
}
```

读法很重要：

| 情况 | `rule_id` | 含义 |
|:--|:--|:--|
| **PASS**（id=1） | `null` | 判词由四值格决定，没有被规则改写；`rules_matched` 说明这条规则**被检查过并且通过** |
| **FAIL，有规则**（id=2） | `"no_file_drift"` | 规则亲手判的，`reason` 里带出 `rule 'no_file_drift' violated` |
| **FAIL，无规则**（id=4） | `null` | 该工具没有规则，回退到四值格 |
| **INVALID，有规则** | `"<rule_id>"` | 规则要求的见证没采集到 |

`null` 不等于"没检查"。`rules_matched` 非空就说明规则加载了、匹配了、并且放行了——
一个 PASS 加上 `"rule_id": null` 和 `"rules_matched": ["no_file_drift"]` 是
"这条规则读过、它没意见"，不是"没有规则"。

**为什么非要记 `rule_id`：** 判词只告诉你结果，不告诉你**谁**判的。
同一个 `INVALID`，可能是"见证根本没采集"，也可能是"策略要求了另一种见证"——
这两种情况的处置方式完全不同，少了 `rule_id` 就分不开。策略会变，trace 不能跟着变糊涂。

## 重点看 id=2

```json
{
  "id": 2,
  "tool": "write_file",
  "check_observed": true,
  "check_source": "protocol",
  "effect_observed": false,
  "verdict": "FAIL",
  "rule_id": "no_file_drift",
  "rules_violated": ["no_file_drift"],
  "reason": "rule 'no_file_drift' violated: expectation 'file_modified' not met (false completion: report says success, witness says no effect) (expected but untouched: missing_dir/target.py) [tool layer reported success, underlying command exited 1]",
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

## 炎症风暴：id=5..7

id=5..7 是 v0.7.0 追加的第二个会话块，演示**同一策略在不同免疫状态下付不同代价**：

| id | 调用 | `tier` | `witness_scope` | `immune_state` | 判词 |
|--:|:--|:--|:--|:--|:--|
| 5 | `write_file missing_dir/target.py` | tier2（规则声明 `high`） | `full` | `NORMAL` | **FAIL** |
| 6 | `append_file no_such_dir/log.txt` | tier1（规则声明 `low`） | `stat` | **`INFLAMED`** | **FAIL** |
| 7 | `append_file log.txt` | **tier2**（同一条 `low` 规则） | `full` | `INFLAMED` | **PASS** |

三件事值得对照：

1. **id=5 是第一次失败**：危险计数 1，未达阈值，`immune_state` 仍是 `NORMAL`——
   这里记的是**本条判词之后**的状态，"还没发炎"本身就是信息。
2. **id=6 是第二次失败**：连续两次 `FAIL`，状态在**这条记录之后**变成 `INFLAMED`。
   注意它自己仍是 `tier1`——**判词不改变产生它的那次调用的代价**，只改变下一次。
3. **id=7 是同一条 `low` 规则**，本该走 tier1，却因为系统已发炎被提到 tier2，
   `changed_files` 里有真实的 git 变更集。**发炎把便宜的路由关掉了。**

`reason` 里也能直接读出来：

```
id=6: ... (expected but untouched: no_such_dir/log.txt)
      [innate tier: existence and mtime only, no git change set]
      [tool layer reported success, underlying command exited 1]
id=7: witness matches intent, report agrees (rules satisfied: append_landed)
```

**为什么 tier1 的判词要自带这句话：** 一个建立在 mtime 上的 `PASS`，和一个建立在
git 变更集上的 `PASS`，是两种强度的断言。trace 如果不写出用的是哪一种，
就等于把每一次廉价的绿灯都说成全量校验——那才是真正的造假。

`witness_mtime` 只在 tier1 有值（那是它的证据本身）；tier2 记录里是 `null`，
因为那一路的证据是变更集，不是时间戳。


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

`rule_id` / `rules_matched` / `rules_violated` 记录的是规则名，与机器无关，不含路径。
`tier` / `witness_scope` / `immune_state` 是枚举值，`witness_mtime` 是浮点时间戳，
同样不含路径。

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

去掉 `--rules none` 就是纯四值格版本，记录里不会有 `rule_id` 三个字段——两种 trace 都合法，
区别在于这一份能回答"这条判词是谁下的"。默认情况下 `harness-audit-server` 会自动加载
包内的 `audit_sidecar/rules/`（`pip install` 之后同样存在），所以复现不需要任何路径参数。

id=3 那条要靠 `reported_success:false` 构造，因为真实工具层不会主动谎报失败——
这一条是为了覆盖判词的另一侧（假失败），不是真实故障的复现。

### 追加的炎症块（id=5..7）

同一个仓库里继续跑三条，第三条就是处于 `INFLAMED` 状态的那次调用：

```bash
printf 'session\n' > log.txt && git add -A && git -c user.name=T -c user.email=t@e commit -qm log

printf '%s\n' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"missing_dir/target.py","content":"value = 5\n"},"reported_success":true},"id":5}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"append_file","args":{"path":"no_such_dir/log.txt","content":"one\n"},"reported_success":true},"id":6}' \
 '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"append_file","args":{"path":"log.txt","content":"two\n"},"reported_success":true},"id":7}' \
 | PYTHONPATH=/path/to/agent-harness-audit python3 -m audit_sidecar.mcp_server --cwd "$D" --quiet

tail -3 "$D/audit_trace.jsonl"
```

id=5 与 id=6 两次 `FAIL` 把状态推到 `INFLAMED`，所以 id=7 虽然是 `risk_level: low`
的追加，也拿到了 git 变更集（`witness_scope: "full"`）。三条记录缺一不可：
**没有前两条失败，第三条的发炎就没有来源。**

## 文件格式

一行一条记录，字段按字母序（`json.dumps(..., sort_keys=True)`），无缩进。
这样追加写入时单次 `write` 不会撕裂记录，`tail -f` 和日志采集器都能安全并行读取。
