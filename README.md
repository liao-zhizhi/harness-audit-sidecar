# agent-harness-audit

**Agent 执行层审计工具**

> **项目状态：MVP / 早期版本（v0.5.0）—— 接口可能变化，暂不建议用于生产环境。**
>
> 已实现四值判词引擎、信任能荷账户、自审计回路、三种见证源与 stdio 服务端，
> 核心闭环（执行 → 见证 → 判词 → 留痕）已跑通并有 156 项测试；
> 但缺少完整 MCP 握手、并发处理与外部 server 转发，且 `reported_success`
> 仍由客户端上送（见「已知边界」）。**欢迎试用与反馈，请勿直接接入关键流程。**
>
> **先看这一份**：[`examples/sample_trace.jsonl`](examples/sample_trace.jsonl)
> —— 一次真实会话里，Agent 收到 `ok` 而账本记 `FAIL` 的现场记录。

本仓库是论文 *From Formal Verification to Agents: A Method-Transfer Audit of
"False Completion" in AI Agent Harnesses* 的工程实现。论文的论点不是"再写一个
agent 框架"，而是把形式验证里"判词"的概念移植到 agent 执行层：**一次动作是否完成，
不能由 agent 自己的报告决定，只能由独立见证（witness）决定**。

> **已实现：** 四值判词引擎、信任能荷账户、自审计回路，**三种见证源**
> （文件系统 / Git 变更集 / MCP 协议层声明），以及**可 `pip install` 的 stdio 服务端**
> （`harness-audit-server`）。
> **未实现：** 完整 MCP 握手（`initialize` / `tools/list`）、从协议帧取声明、
> 更多见证源（mtime / 进程退出码族 / 网络副作用）。详见「已知边界」与「路线图」。

仅依赖 Python 标准库，无任何第三方包。

---

## 示例：一次会话里的四条记录

[`examples/sample_trace.jsonl`](examples/sample_trace.jsonl) 是真实跑出来的一份审计留痕
（非手写）。同一次会话里，Agent 收到的协议层答复与账本记录如下：

| id | 调用 | agent 声明 | 真实效果 | 判词 |
|--:|:--|:--|:--|:--|
| 1 | `write_file target.py` | 成功 | 已写入 | **PASS** |
| 2 | `write_file missing_dir/target.py` | 成功 | 无变化 | **FAIL**（工具层吞错） |
| 3 | `write_file target.py` | 失败 | 已写入 | **FAIL**（假失败） |
| 4 | `run_command`（期望改 `target.py`） | 成功 | 改的是 `wrong.py` | **FAIL**（目标漂移） |

四条里三条 FAIL，而 Agent 只被明确告知一次失败。id=2 的账本记录：

```json
{"id":2,"tool":"write_file",
 "check_observed":true,"check_source":"protocol","effect_observed":false,
 "verdict":"FAIL","exit_code":1,
 "reason":"false completion: report says success, witness says no effect (...)
           [tool layer reported success, underlying command exited 1]"}
```

`check_observed: true` 是工具层对 Agent 说的话，`effect_observed: false` 是 git 看到的实情，
`exit_code: 1` 是命令自己的状态。**三套观测同时留档**，才不会在事后搞错该怀疑谁。

完整解读、脱敏说明与复现命令见 [`examples/README.md`](examples/README.md)。

---

## 为什么需要它

一个 agent 说"已完成"，背后有三种完全不同的世界：

| 世界 | agent 报告 | 磁盘真相 | 布尔判词会怎么说 |
|:--|:--|:--|:--|
| 真完成 | 成功 | 落地 | 完成 ✓ |
| **假完成** | 成功 | **没落地** | 完成 ✓ ← **撒谎** |
| 未采集 | 成功 | **不知道** | 完成 ✓ ← **假装知道** |

第二、三行是布尔判词的结构性缺陷：它没有词汇去表达"我根本没看"。四值判词把它拆开：

- **FAIL** —— 我看了，看到的是失败（假完成落在这一类）。
- **INVALID** —— 我没看。**不许说 PASS**。

---

## 目录结构

```
agent-harness-audit/
├── README.md
├── audit_sidecar/
│   ├── __init__.py       公开接口
│   ├── verdict.py        四值判词引擎
│   ├── confidence.py     信任能荷账户（滑动窗口）
│   ├── runner.py         见证采集（真实执行 + 独立读回）
│   ├── git_witness.py    Git 见证（真实变更集）
│   ├── mcp_proxy.py      MCP 拦截（协议层声明 vs 物理效果）
│   ├── mcp_server.py     stdio JSON-RPC 服务端（console script）
│   ├── self_audit.py     自审计（mode 4 防线）
│   └── cli.py            命令行入口
├── pyproject.toml        打包配置（零运行时依赖）
├── .gitignore
├── examples/
│   ├── sample_trace.jsonl   真实审计留痕（Agent 收到 ok / 账本记 FAIL）
│   └── README.md            样例说明
└── tests/
    ├── test_verdict.py
    ├── test_confidence.py
    ├── test_runner.py
    ├── test_git_witness.py
    ├── test_mcp_proxy.py
    └── test_e2e_stdio.py   端到端（真子进程 + 真管道）
```

---

## 快速开始

```bash
cd agent-harness-audit

# 1. 跑测试
python3 -m unittest discover tests

# 2. 跑 demo（启动时先跑自审计，不通过则拒绝启动）
python3 -m audit_sidecar.cli

# 3. 装成可执行的 stdio sidecar
pip install .
harness-audit-server --cwd /path/to/project
```

编程式使用：

```python
from audit_sidecar import ConfidenceAccount, Verdict, VerdictEngine

engine = VerdictEngine()

# 意图是写入新内容；见证（文件哈希）显示还是旧内容；agent 报告成功
verdict = engine.evaluate(
    intent="sha256:new",
    effect="sha256:old",
    check=True,
)
assert verdict is Verdict.FAIL        # 假完成

account = ConfidenceAccount(window=100)
account.record(verdict)
account.get_balance()                 # 0.0
account.get_label()                   # "low"
```

跑一条真实命令并取见证（详见下文「见证采集」）：

```python
from audit_sidecar import SafeRunner

result = SafeRunner().run_shell(
    "echo 'feature_x=on' >> config.yaml",
    cwd="/tmp/project",
    intent_desc="append feature_x=on to config.yaml",
    expected_file="config.yaml",
    expected_content="feature_x=on",
)
print(result.verdict, result.effect_observed, result.check_observed)
```

---

## 判词规则（`verdict.py`）

三个输入：

| 参数 | 含义 | 谁提供 |
|:--|:--|:--|
| `intent` | 预期动作的可比较表示（如"写入后应有的哈希"） | 任务定义 |
| `effect` | **独立于 agent** 观测到的副作用；`None` 表示未采集 | 见证采集器 |
| `check` | agent 自己读到的状态（bool / 状态串 / 带 `.success` 的对象） | agent 自报 |

规则按顺序匹配，**先命中先返回**：

| # | 条件 | 判词 | 含义 |
|:--:|:--|:--|:--|
| 1 | `effect is None` | `INVALID` | 没有见证。**不返回 PASS**，即使 `check=True` |
| 2 | `intent is None` | `NOT RUN` | 无意图记录，该场景未执行 |
| 3 | `effect != intent` | `FAIL` | 意图未实现；若 `check` 报成功，即**假完成** |
| 4 | `check` 报失败 | `FAIL` | **假失败**：副作用已落地，agent 却报错 |
| 5 | 其余 | `PASS` | 见证与报告一致 |

两条刻意的设计决定：

1. **三个参数强制 keyword-only。** `effect` 与 `check` 形状相同，位置参数一旦写反，
   会得到一个"看起来很合理"的错判词而不报错。审计工具不能有这种静默失败。
2. **枚举成员名是 `NOT_RUN`，值是 `"NOT RUN"`。** Python 标识符不能含空格，
   值保留论文原始拼写。

另外 `engine.explain(...)` 返回 `(Verdict, reason)`，reason 是一句可机检的英文说明，
便于把判词写进日志而不只是打个标签。`evaluate` 与 `explain` 共用同一段判定逻辑，
不存在两套规则漂移的可能。

`check` 的归一化只接受**可明确解释**的报告；无法识别的字符串（如 `"probably fine"`）
会抛 `ValueError` 而不是被猜成成功。猜，正是这个工具要消灭的行为。

---

## 信任能荷账户（`confidence.py`）

```python
account = ConfidenceAccount(window=100)   # 默认 100
account.record(verdict)
account.get_balance()   # PASS / (PASS + FAIL + INVALID)
account.get_label()     # high / medium / low
```

- 滑动窗口保留最近 N 次判词，旧事件自动淘汰。
- **`NOT RUN` 占窗口格位，但不进分母**：它不携带关于 harness 的信息，不该稀释比例。
- `PASS / (PASS+FAIL+INVALID)`：
  - `>= 0.7` → `high`
  - `[0.5, 0.7)` → `medium`
  - `< 0.5` → `low`
- 窗口内全无有效事件时 `balance = 0.0`、`label = low`：**没证据 ≠ 可信**。

---

## 见证采集（`runner.py`）

`SafeRunner` 真实执行 Shell 命令，然后**自己去文件系统读回结果**，不采信命令自己的说法。

```python
from audit_sidecar import SafeRunner

runner = SafeRunner()
result = runner.run_shell(
    "echo 'feature_x=on' >> config.yaml",
    cwd="/tmp/project",
    intent_desc="append feature_x=on to config.yaml",
    expected_file="config.yaml",
    expected_content="feature_x=on",
)

result.check_observed    # True   —— bash 退出码（pipefail 之下）
result.effect_observed   # True   —— 从文件真实读出来的，与退出码无关
result.verdict           # Verdict.PASS
```

`ExecutionResult` 的字段：`intent`（意图描述）、`effect_observed`（`True`/`False`/`None`）、
`check_observed`、`verdict`，外加 `exit_code`、`pipe_status`、`witness_path`、`reason`、
`stdout`、`stderr`、`error` 作为取证留痕。

### 防线一：管道吃掉状态（mode 1）

`false | cat` 在默认 bash 里退出 **0**——最后一个 stage 成功了，前面失败没人管。
一个把活儿管过 `| head`、`| tee`、`| grep` 的 agent 可以因此藏住失败而**并没有撒谎**。

`SafeRunner` 用 `set -o pipefail` 执行，并把每段的 `PIPESTATUS` 记进 `pipe_status`：

```
$ bash -c 'false | cat'; echo $?            # 天真读法
0
$ bash -o pipefail -c 'false | cat'; echo $?  # 审计读法
1
```

> 实现细节：命令写在临时 wrapper 脚本里执行，而不是 `bash -c`。这样 `PIPESTATUS`
> 能在**任何后续命令之前**被读走；同时 wrapper 显式 `exit` 成管道状态，
> 否则最后那句 `printf` 会把退出码覆盖成 0。

### 防线二：见证坐标漂移（mode 3 变体）

这是更隐蔽的一类：agent 在**错误的目录**里干活，而审计也去**同一个错误目录**里查证据。
它会查到，然后给一个自信的 PASS——工作从未到达目标。

`witness_root` 就是为此存在的：命令的 `cwd` 与见证的坐标**分开指定**。

```python
# agent 跑在 drift/，但意图的坐标是 project/
result = runner.run_shell(
    "echo 'feature=on' >> config.yaml",
    cwd=drift,                                  # 动作在这里发生
    expected_file="config.yaml",
    witness_root=project,                       # 见证在这里读取
    expected_content="feature=on",
)
result.verdict   # Verdict.FAIL —— 假完成
```

`expected_file` 给绝对路径时直接使用，忽略 `witness_root`。

### 真实判决（`python3 -m audit_sidecar.cli` 的 [3] 段）

| 案例 | 命令 | exit | PIPESTATUS | effect | check | 判词 |
|:--|:--|--:|:--|:--|:--|:--|
| A1 管道吃状态 | `false \| cat` | 1 | `1 0` | `None` | False | **INVALID** |
| A2 同上 + 见证 | `false \| cat > result.txt` | 1 | `1 0` | False | False | **FAIL** |
| B1 见证跟着漂 | `echo 'feature=on' >> config.yaml`（cwd=drift，见证 drift） | 0 | `0` | True | True | **PASS（审计失明）** |
| B2 见证钉住意图 | 同上，见证 project | 0 | `0` | False | True | **FAIL** |

四点值得注意：

- **A1 不是 PASS，也不是靠"猜"变成 FAIL。** 退出码说失败，但没指定任何产物，
  于是 `effect` 是 `None`，引擎只能给 `INVALID`。**拿不到见证，就不给结论**——
  这正是四值判词和布尔判词的分界。
- **A2 才是真正的 FAIL。** 产物存在但是空的，意图没实现；`pipefail` 让退出码与见证一致。
- **B1 的 PASS 是审计失明，不是 agent 正确。** `project/config.yaml` 从头到尾没被碰过。
  同一件事在 B2 里被抓住，差别只在见证读哪里。
- 想抓漂移，**见证必须钉在意图的坐标上**。让见证跟着动作走，等于让嫌疑人自己保管物证。

### 失败路径

| 情形 | `effect` | 判词 | 理由 |
|:--|:--|:--|:--|
| `cwd` 不存在 / 无法启动 | False | `NOT_RUN` | 命令根本没跑；`INVALID` 留给"跑了但没有见证" |
| 超时被 kill | `None` | `INVALID` | kill 后的文件状态是中间态，不能当证据 |
| 未指定 `expected_file` | `None` | `INVALID` | 没让看，就没看 |
| 命令含顶层 `exit` | 正常读 | 正常判 | wrapper 提前终止，`pipe_status` 回退为 `(exit_code,)` |



## Git 见证（`git_witness.py`）

文件存在性只能证明"某个文件被写了"，证明不了"**写对了那个文件**"。Mode 3 最常见的形态是
agent 在错误的坐标里干活，然后读回自己刚写的文件、报告成功——它的自述和它的读回**完全自洽**，
只是都不在目标上。

Git 是唯一一份 agent 没有参与编写的记录。`GitWitness` 读它。

```python
from audit_sidecar import GitWitness

witness = GitWitness("/path/to/repo")
witness.get_changed_files()      # ['wrong_dir/target.py']  仓库根相对、POSIX、已排序
witness.repo_root                # '/path/to/repo'
```

底层是 `git status --porcelain=v1 -z --untracked-files=all --no-renames`：

- **`-z`**：不转义文件名，避免引号把路径变成另一个字符串。
- **`--untracked-files=all`**：新建目录时列出里面的文件。漂移通常长这样，只报目录名会漏。
- **`--no-renames`**：重命名不产生"两条路径一条记录"，解析时不会吃掉下一条。
- 覆盖已暂存、未暂存、未跟踪、已删除；被 `.gitignore` 忽略的不出现（git 不报）。

### 两个必须自己解决的问题

**坐标。** `git status` 报的是**仓库根相对**路径。命令在 `sub/` 里正确地改了 `sub/target.py`，
git 说 `sub/target.py`，而调用方以为 `target.py` —— 直接比较会**把一次正确的编辑判成漂移**。
`resolve_expected()` 先按命令的 `cwd` 把期望路径映射进仓库根坐标。这条规则和 `witness_root`
是同一个教训：**比较两个东西之前，先确认它们在同一个坐标系里**。

**基线。** 工作区在执行命令前通常就是脏的。事后取一次快照无法区分"这条命令改的"和
"本来就这样"。`snapshot()` 给每个脏路径记一个内容指纹，`changed_since(baseline)` 只报
**新增或内容真的变了**的路径。没有这一步，一个长期工作的仓库会把所有历史改动都当成这次的证据。

```python
baseline = witness.snapshot()
# ... 执行命令 ...
witness.changed_since(baseline)   # 只含这次命令造成的变化
```

### 接入 `SafeRunner`

```python
result = runner.run_shell(
    "mkdir -p wrong_dir && echo 'value=1' > wrong_dir/target.py",
    cwd="/repo",
    intent_desc="edit target.py",
    expected_files_modified=["target.py"],   # 相对 cwd
)
result.changed_files      # ('wrong_dir/target.py',)
result.missing_expected   # ('target.py',)
result.unexpected_changes # ('wrong_dir/target.py',)
result.verdict            # Verdict.FAIL
```

- 基线在命令执行**之前**自动采集。
- `expected_files_modified` 相对 `cwd`；`cwd` 在仓库外时（配合 `repo_path`）相对仓库根。
- 与 `expected_file` / `expected_content` 同时给出时是**合取**：任一证人说不，就不给 PASS。
- 若同时给了产物见证，那个产物文件本身不算"意外改动"。

### 严格 vs 宽松：默认取严格

| 模式 | 判定 | 场景 |
|:--|:--|:--|
| `allow_extra_changes=False`（默认） | 变更集必须**恰好等于**期望集 | 改对了 A 还顺手改了 B → FAIL |
| `allow_extra_changes=True` | 期望集是变更集的**子集** | 允许构建产物、格式化等附带改动 |

默认取严格，因为"改对了还多改别的"比"没改"更危险，也更难在事后发现。
> 注：这两种模式在你的需求里被同时描述过（"子集即 True" 与 "改了期望之外的文件即 False"），
> 对"污染"案例给出相反结果。我按后者实现为默认，前者保留为显式开关。

### Case C 的真实输出（`cli.py` 的 [4] 段）

```
    case                             verdict  changed (from git)                 effect  check
    -------------------------------- -------- ---------------------------------- ------- ------
    C1  correct edit                 PASS     target.py                          True    True
    C2  edited wrong_dir/target.py   FAIL     wrong_dir/target.py                False   True
    C3  edited target.py + other.py  FAIL     other.py, target.py                False   True
```

**C2 是要点**：agent 的退出码说成功，它自己读回 `wrong_dir/target.py` 也确实读到了正确内容——
两处自洽，却都不在目标上。只有 git 知道真相，因为 git 是 agent 没有编写过的那份记录。
C3 演示严格默认：多改一个无关文件也算漂移，不是加分项。

### 失败路径

| 情形 | 行为 |
|:--|:--|
| `repo_path` 不是 git 仓库 | 抛 `GitWitnessError`，**命令不会执行** |
| `git` 不在 PATH | 抛 `GitWitnessError`（不静默降级成"无变更"） |
| 期望路径在仓库外 | 抛 `ValueError` |
| 传了裸字符串而非列表 | 抛 `TypeError`（`"abc"` 会被当成三个路径遍历） |
| 期望集为空 | 抛 `ValueError`（用 `expected_file` 或干脆不传） |

**绝不静默降级**：看不了就说看不了。一个把"无法观测"报成"没有变更"的见证，比没有见证更糟。



## MCP 拦截（`mcp_proxy.py`）

前三层都在审计**命令**。这一层审计**工具层**——这是完全不同的两个被告。

在真实 harness 里，命令的退出码先被工具层看到，Agent 只看到工具层的答复。如果封装层吞掉
一个失败并回 `success`，Agent 会**真诚地**错下去：它的自述与它收到的一切完全自洽，整段
对话里没有一个字是谎话。这类假完成最难查，因为无处可查。

`MCPProxy` 坐在 Agent 与工具层之间：

```
Agent ──tool_call──▶ MCPProxy ──▶ 工具层 ──▶ 真实命令
   ▲                    │                        │
   └──── 原样返回 ───────┘                        ▼
        （不修正 Agent 的认知）        独立见证：GitWitness
                        │
                        └──▶ VerdictEngine(intent, effect=物理见证, check=协议层声明)
                             └──▶ 内部审计日志（Agent 看不到）
```

```python
from audit_sidecar import MCPProxy

proxy = MCPProxy(cwd="/repo")

# agent_reported_success 是工具层递给 Agent 的状态，即"被审计的声明"
response = proxy.intercept_tool_call(
    "write_file",
    {"path": "missing_dir/target.py", "content": "x"},
    agent_reported_success=True,      # 工具层说成功了
)
response            # {'status': 'ok', 'isError': False, ...}  ← Agent 以为没事
proxy.audit_log[-1].verdict    # Verdict.FAIL                   ← 审计记录不同意
```

### 两条不可让步的原则

**一、`check` 必须来自协议层，不能从退出码重新推导。**

这是本模块存在的唯一理由。图省事写成 `check = reported_success and exit_code == 0`，
就等于把工具层的答复替换成工具层自己看到的退出码——**让被告给自己作证**。
实测这个"洗白"会真的翻转判决：

| 场景 | 正确实现（协议层声明为 check） | 从退出码推导（错误实现） |
|:--|:--|:--|
| 工具报 success，命令退出 0，但文件没变 | `FAIL` 假完成 | `FAIL` 假完成（凑巧同判） |
| 工具报 success，命令退出 1，文件没变 | `FAIL` 假完成 | `FAIL` 意图未实现（**诊断错**） |
| **工具报 failure，命令退出 0，文件真的改了** | **`FAIL` 假失败** | **`PASS`** ← 说谎的工具被洗白 |

最后一行是要害。`test_check_is_not_rederived_from_exit_status` 直接断言
`check_source == "protocol"`、`check_observed is True` 且 `exit_code != 0`，
一旦有人改动推导方式，测试立刻失败。

**二、代理是被动的。**

返回给 Agent 的响应**原样**反映工具层的声明，绝不掺入见证结论。`MCPProxy` 不纠正 Agent 的
认知，只把分歧记在自己的日志里。一个会替 Agent 更正的"审计器"就不再是观察者，而变成了
被审系统的一部分——它一旦出错，就没有第二双眼睛了。

`test_response_stays_passive` 钉住这条：谎言场景下响应仍是 `status='ok'`，
且内容里不出现 `FAIL` 或 `witness` 字样。

### Case D 的真实输出（`cli.py` 的 [5] 段）

```
    case                               exit  agent check  actual effect  verdict
    ---------------------------------- ----- ------------ -------------- --------
    D1  write_file, honest tool        0     True         True           PASS
    D2  tool swallowed a path error    1     True         False          FAIL
    D3  tool under-reported success    0     False        True           FAIL
```

D2 就是你要看的那个：**`Agent Check: True` / `Actual Effect: False`**。原因串还顺手把工具层
的自我矛盾点出来：

```
false completion: report says success, witness says no effect
(expected but untouched: missing_dir/target.py)
[tool layer reported success, underlying command exited 1]
```

D3 是另一侧：写入真的落地了，工具层却报错。Agent 会以为自己失败了，然后重试——
一个"假失败"引发的重复写入，通常比假完成更难善后。

### 参数安全

工具参数会被拼进真实执行的 shell 命令，所以**每个插值都过 `shlex.quote`**。
否则被审计的内容就能变成代码：`content="x; rm -rf /"` 会先写文件再执行。

`tests/test_mcp_proxy.py` 用三个载荷钉住这条（`evil; touch PWNED`、
`$(touch PWNED2)`、`` `touch PWNED3` ``），断言文件内容与载荷**逐字节相同**且 PWNED 文件不存在。

### 失败路径

| 情形 | 行为 |
|:--|:--|
| 未知工具名 | 抛 `UnknownToolError`，**不执行、不记入日志**（拒绝伪造审计记录） |
| `agent_reported_success` 不是 bool | 抛 `TypeError`（`None` 也拒绝：请用 `False` 表示"没有状态到达 Agent"） |
| 缺参数 / 参数非字符串 | 抛 `ValueError` / `TypeError` |
| `expected_files_modified` 传裸字符串 | 抛 `TypeError` |

**未知工具为什么拒绝而不是返回错误响应**：返回一个通用错误、日志里什么都不记，等于让
未经审计的调用无声累积。一个"看起来很尽责但有盲区"的拦截层，比没有拦截层更危险——
因为调用方以为它在看着。生产版应改为"记录 + 放行"，以免打断 Agent 流程；MVP 选择响亮失败。



## 作为 Sidecar 接入（stdio）

安装：

```bash
pip install .
# 或本地构建：python3 -m pip wheel . --no-deps -w dist/
```

装好后得到一个可直接启动的 stdio 服务：

```bash
harness-audit-server --cwd /path/to/project
# 等价于：python3 -m audit_sidecar.mcp_server --cwd /path/to/project
```

| 参数 | 默认 | 说明 |
|:--|:--|:--|
| `--cwd` | `.` | 被审命令的执行目录 |
| `--repo` | 同 `--cwd` | Git 见证使用的仓库根 |
| `--trace` | `<cwd>/audit_trace.jsonl` | 审计留痕（JSONL） |
| `--timeout` | `30` | 单条命令超时（秒） |
| `--quiet` | 关 | 静默 stderr 诊断 |

### 协议

换行分隔的 JSON-RPC 2.0，一行一个对象（多数 stdio MCP 传输的行模式即如此）。

**请求**

```json
{"jsonrpc":"2.0","method":"tool_call",
 "params":{"tool":"write_file",
           "args":{"path":"target.py","content":"v = 1\n"},
           "reported_success":true},
 "id":1}
```

**响应**（Agent 看到的就是这个）

```json
{"jsonrpc":"2.0","id":1,
 "result":{"status":"ok","isError":false,
           "content":[{"type":"text","text":"write_file: ok"}]}}
```

**审计留痕**（Agent 看不到，写进 `audit_trace.jsonl`）

```json
{"id":1,"tool":"write_file","intent":"modify file target.py",
 "agent_reported_success":true,"check_observed":true,"check_source":"protocol",
 "effect_observed":true,"verdict":"PASS","reason":"witness matches intent, report agrees",
 "exit_code":0,"pipe_status":[0],"changed_files":["target.py"], ...}
```

### 接入 Agent 客户端

任何支持"把工具调用转发给一个 stdio 子进程"的框架都能接。以常见的 MCP 客户端配置为例：

```json
{
  "mcpServers": {
    "harness-audit": {
      "command": "harness-audit-server",
      "args": ["--cwd", "/path/to/your/project", "--quiet"]
    }
  }
}
```

把原来直连真实工具的客户端改为连到本代理即可。要点：

1. `--cwd` 必须是**被审项目**的目录，不是代理自己的目录。
2. 如果是 git 项目，`--repo` 一般留空（取 `--cwd`）；跨目录时显式指定。
3. 轮询 `audit_trace.jsonl` 做告警/仪表盘；**不要**从 stdout 取判词——那里只有 Agent 该看到的东西。
4. 判词文件按行追加，可安全地被 `tail -f` 或日志采集器并行读取。

### 实测：装完就能拦

```bash
$ pip install .
$ printf '%s\n' \
  '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"ok.py","content":"fine"},"reported_success":true},"id":1}' \
  '{"jsonrpc":"2.0","method":"tool_call","params":{"tool":"write_file","args":{"path":"nope/ok.py","content":"lie"},"reported_success":true},"id":2}' \
  | harness-audit-server --cwd /repo --quiet
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", ...}}
{"jsonrpc": "2.0", "id": 2, "result": {"status": "ok", ...}}    ← Agent 两次都被告知成功
$ cat /repo/audit_trace.jsonl
  id=1 check=True(protocol) effect=True  -> PASS
  id=2 check=True(protocol) effect=False -> FAIL   ← 谎言在这里
```

### 协议层错误

| 情形 | JSON-RPC 错误码 |
|:--|:--|
| 非法 JSON | `-32700` parse error |
| 不是 JSON 对象 / `jsonrpc` 非 `2.0` | `-32600` invalid request |
| `method` 不是 `tool_call` | `-32601` method not found |
| `params` 缺字段 / 类型不对 / **缺 `reported_success`** | `-32602` invalid params |
| 未知工具名 | `-32001`（应用码，**不执行、不记 trace**） |
| 执行期 `OSError` | `-32603` internal error |

三条刻意的选择：

- **`reported_success` 缺失时拒绝，不默认 `True`。** 默认值会凭空造出"被审计的那个声明"，
  于是每一次调用都会判 PASS——审计变成盖章。
- **通知（无 `id`）不回响应。** JSON-RPC 明确禁止；回了会让客户端状态机错位。
  但**仍然执行并记账**。
- **trace 写失败不中断服务。** 只报 stderr。一个写不了日志就拒绝服务的审计器，
  会把 Agent 一起拖下水；日志故障是事故，不是理由。



## 自审计（`self_audit.py`）

论文失败模式 **mode 4** 是"审计器本身变成了装饰品"：它永远返回 PASS（或永远返回 FAIL），
而没人发现，因为审计器从不被审计。

`run_self_audit()` 用**答案已知**的样本喂给引擎：

| 样本 | 构造 | 期望 |
|:--|:--|:--|
| known-bad | 假装写文件（真的没写），哈希仍是旧值，agent 报成功 | `FAIL` |
| known-good | 真的写入，哈希已变，agent 报成功 | `PASS` |
| no-witness | agent 报成功，但 `effect=None` | `INVALID` |

只喂 known-bad 是不够的——一个"永远 FAIL"的引擎能通过那条检查。所以后两个样本
不是陪衬，它们把"永远 PASS"和"永远 FAIL"两种退化都堵死：

```
$ python3 -c "..."    # 把 VerdictEngine 换成永远返回 PASS / FAIL 的桩
baseline                 : True
always-PASS engine       : False
always-FAIL engine       : False
```

`cli.py` 在启动时先调用它；不通过则打印
`Self-audit failed, tool is a decoration.` 并 `exit(1)`，**拒绝启动**。

样本用 `tempfile` 真写文件、真算 SHA-256，不是硬编码的假数据；
若文件系统行为异常导致样本本身不成立（哈希未变/未落地），自审计同样返回 False。

---

## 测试

```bash
python3 -m unittest discover tests -v
# Ran 156 tests ... OK
```

覆盖：

- **判词**：四种判词各自的触发场景（重点 `INVALID` 不许变 `PASS`、`FAIL` 的假完成/假失败/
  意图未实现三种分支）、判词优先级、`check` 归一化与拒绝、keyword-only 约束。
- **账户**：窗口淘汰、`NOT RUN` 占格但不入分母、label 阈值边界（0.7 / 0.5）。
- **文件见证**：`false | true` 的退出码不被吞、`PIPESTATUS` 逐段记录、天真读法与审计读法的
  差值、内容校验、缺失产物判假完成、坐标漂移（钉住 / 跟随见证两种对照）、超时与不存在的 `cwd`、
  临时 wrapper 不落到工作目录。
- **Git 见证**：全部在**真实临时仓库**里跑真 `git`（`skipUnless(git 与 bash 可用)`），
  覆盖修改 / 未跟踪 / 新建子目录 / 删除 / 已暂存、排序与 POSIX 路径、`.gitignore` 不出现、
  非仓库报错、基线排除既有脏改动、同一文件二次编辑、子目录坐标映射、`repo_path` 选中仓库、
  严格与宽松两种污染策略、两个见证合取。
- **MCP 拦截**：诚实调用判 PASS、工具吞错判 FAIL、`check` 不得从退出码重推导（回归守卫）、
  原因串点出工具层自相矛盾、响应保持被动、假失败、静默工具（`true` 却报成功）、
  未知工具拒绝且不执行、参数注入防护（三种载荷）、含空格路径、契约校验。
- **端到端**：`subprocess` 真启动子进程、真写 stdin、真读 stdout。验证响应是合法
  JSON-RPC 且**不泄漏判词**、`audit_trace.jsonl` 记录正确判词（含 `FAIL`）、
  一次会话多调用、字符串 id 回显、非法 JSON / 未知方法 / 版本错 / 缺 `reported_success`
  / 非 bool 声明各自的错误码、通知不回响应但**仍执行并记账**、坏请求后继续服务、
  trace 默认落在 `--cwd`、跨会话追加、启动参数错误退出码为 2。
- 全部用 `tempfile.TemporaryDirectory()`，不留垃圾（含 git 临时仓库）。

---

## 已知边界（MVP 范围）

- **没有 Content-Length 分帧。** stdio 传输用换行分隔 JSON；MCP 规范的另一种
  Content-Length 模式未实现，接第三方客户端前需确认对端用哪种。
- **没有握手与能力协商。** 不实现 `initialize` / `tools/list` / `notifications`；
  只有 `tool_call`。真实 MCP 客户端握手会失败，需要一层适配。
- **工具注册表是内置的。** 只有 `write_file` / `append_file` / `run_command` 三个，
  工具实现由本仓库生成（命令拼接），不是转发到外部 MCP server。
- **`reported_success` 仍由客户端传入。** 这是本层最大的诚实性缺口：它守护
  "工具层 vs 物理世界"，但"客户端 vs 工具层"这一段没有见证。真正的拦截应从协议帧的
  `result.isError` 解析，而不是相信上送的值。
- **单进程、单线程、无并发。** 一行一请求串行处理；慢命令会阻塞后续调用。
  无批处理（JSON-RPC batch 未实现）。
- **见证只有文件系统与 git**。没有 mtime 精度处理、没有进程退出码族、没有网络副作用；
  `git` 见证依赖工作树状态，对已 commit 的历史改动不可见（基线是工作区快照）。
- **不阻止坏动作。** 没有 hook、没有代理；本工具是旁路判词器，不阻止坏动作发生，
  只拒绝为它背书。
- **`pipe_status` 对顶层 `exit` 退化。** 命令里出现顶层 `exit` 会让 wrapper 提前终止，
  此时拿不到逐段状态，回退为 `(exit_code,)`；判词不受影响。
- **超时后的状态不采信。** 进程被 kill，文件可能是中间态，因此判 `INVALID` 而不是
  `FAIL`。保守方向。
- **git 见证只认工作区**。若命令自己 `git commit` 了，改动就从未提交集合里消失，
  严格模式下会表现为"期望文件没被改"。这是刻意的保守选择，不是遗漏。
- **trace 无轮转、无锁。** 只追加不切割；多进程写同一文件时依赖单次 `write` 的原子性
  （小于 PIPE_BUF 量级通常安全），未做显式加锁。
- **`intent` 会原样回显客户端路径。** `changed_files` / `missing_expected` /
  `unexpected_changes` 都是仓库根相对路径（`git status` 坐标系），但 `intent` 记录的是
  调用参数的原文。客户端传绝对路径，本机路径就会进 trace。分享审计日志前请自查：
  `grep -cE '/tmp/|/home/' audit_trace.jsonl`。
- **无持久化。** 账户与审计日志都在内存里，进程退出即丢（trace 文件除外）。

## 路线图

1. **见证采集**：
   - 文件系统：已具备（存在性 + 内容子串）。
   - Git 变更集：已具备（`git_witness.py`）。
   - 待扩展：mtime、内容哈希、进程退出码族、网络副作用。
2. **MCP 拦截**（语义与传输已具备，握手未接）：
   - 已具备：`MCPProxy` 的调用边界、被动响应、工具注册表、注入防护；
     `mcp_server.py` 的 stdio JSON-RPC 服务端与 JSONL 审计留痕。
   - 待扩展：从协议帧的 `result.isError` 取声明而非由客户端上送、`initialize` 握手与
     `tools/list`、Content-Length 分帧、转发到外部 MCP server、并发与批处理。
3. 账户持久化与跨会话聚合；把判词流导出为可复核的审计日志。

## 许可

代码 MIT；论文文本 CC BY 4.0。
