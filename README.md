# agent-harness-audit

**Agent 执行层审计工具**

> **项目状态：MVP / 早期版本（v0.8.0）—— 接口可能变化，暂不建议用于生产环境。**
>
> 已实现四值判词引擎、信任能荷账户、自审计回路、三种见证源、外置判据库、条件触发的
> 分级免疫响应（含跨进程持久化）、引用见证（DOI/arXiv 物理解析）与 stdio 服务端，核心闭环
> （执行 → 见证 → 判词 → 留痕）已跑通并有 348 项测试；
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
> （文件系统 / Git 变更集 / MCP 协议层声明）、**外置判据库（`rules/*.yaml`）**、
> **条件触发的分级免疫响应**（先天 stat / 适应性全量，含炎症风暴与恢复），
> 以及**可 `pip install` 的 stdio 服务端**（`harness-audit-server`）。
> **未实现：** 完整 MCP 握手（`initialize` / `tools/list`）、从协议帧取声明、
> 更多见证源（进程退出码族 / 网络副作用）、跨进程持久化炎症状态。
> 详见「已知边界」与「路线图」。

审计逻辑本身只用 Python 标准库；**唯一的第三方依赖是 PyYAML**，用来把
*策略*从代码里搬出去（见 [外置判据库](#dynamic-judgment-base-外置判据库)）。

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

文件末尾还有第二个会话块（id=5..7），演示发炎后同一工具的代价升级，
见[条件触发的免疫响应](#conditional-immune-response-条件触发的免疫响应)。

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

## Dynamic Judgment Base (外置判据库)

判词以前全部长在代码里。这有个具体的坏处：**你没法 diff 一条规则，也就没法争论它。**
想收紧某个工具的验收标准，得改 Python、发版本；实际结果就是没人收紧。

外置判据库把**每个工具的策略**搬进 YAML：

```yaml
# audit_sidecar/rules/write_file.yaml（随包发布）
rule_id: "no_file_drift"
target_tool: "write_file"
require_witness: true
witness_type: "git_diff"
expectation: "file_modified"
```

改动验收标准从此是一次 `rules/` 里的 diff，看得懂的人不必是写代码的人。
默认规则在包内，所以"没配规则"这种状态不存在——不配就是这份。

### 它到底多做了什么

注意 `require_witness` 这一条：**四值格表达不了它**。文件见证已经证明写入落地了，
格子的判断是对的——`PASS`。可策略说："光有文件见证不够，我要 git 变更集。"

同一份观测，两种判词：

| 观测 | 四值格 | 加规则 | `rule_id` |
|:--|:--|:--|:--|
| 只有文件见证 | `PASS` | **`INVALID`** | `no_file_drift` |
| 文件 + git 见证 | `PASS` | `PASS` | `null`（规则检查过，放行） |
| 写入没落地 | `FAIL` | `FAIL` | `no_file_drift` |
| 工具没有规则 | `PASS` | `PASS` | `null`（回退到格子） |

第一行就是升级的意义：它说的不是"写入失败"，而是"**这次成功不算证据**"。
这句话在没有任何规则的代码里写不出来。

### 单调性：规则只能收紧，不能放松

这是硬约束，不是实现细节：

```
NOT RUN  ←  什么都没跑，规则无从违反，原样保留
FAIL     ←  见证与记录矛盾。缺一个见证并不能把矛盾解释掉
INVALID  ←  规则要求的见证没采集到。没有证据就不许说 PASS
PASS     ←  格子同意，且规则全部满足
```

**一条能把 `FAIL` 变成 `PASS` 的规则，就是一种跟见证讨价还价的方式**——而那正是这套
工具存在的理由。`tests/test_rule_engine.py::TestMonotonicity` 把这条钉死了。

### 加载是严格的

未知字段、未知的 `witness_type`、未知的 `expectation`、跨文件重复的 `rule_id`、
`require_witness: true` 却没写 `witness_type`——**全部在加载时报错**。

理由和自审计一样：一条你以为是活的、其实是死的规则，比没有规则更糟。所以宁可炸掉：

```
RuleError: audit_sidecar/rules/write_file.yaml: unknown field(s) ['require_witnes'];
the loader refuses to ignore a guard it does not understand
```

### 怎么用

```bash
# 装好即生效：包内默认规则会自动加载，不需要 --rules
harness-audit-server --cwd /path/to/repo

# 想用项目自己的策略：放到 ./rules 下，自动优先于包内默认值
harness-audit-server --cwd /path/to/repo --rules ./rules

# 明确要求"只用四值格"，而不是碰巧没找到规则
harness-audit-server --cwd /path/to/repo --rules none
```

```python
from audit_sidecar import JudgmentBase, RuleEngine, MCPProxy

proxy = MCPProxy(cwd=repo, rules_dir="rules")   # 库默认不加载规则，显式选择才生效
```

服务端与库的默认值不同，是有意的：**服务端**装完就该拦，所以自动加载包内规则；
**库**的调用方可能只是要四值格，所以不传 `rules_dir` 就是纯四值格。`--rules none`
与"自动探测没找到"是两件事：前者是操作者的选择，后者是意外。

### 自审计跟着升级

判据库让策略离开了代码，也就让**失效模式**离开了代码。所以自审计多了一组已知答案的
样本，检查三件事：规则**确实加载了**、违规的运行**被判 FAIL/INVALID**、判词**说出了
是哪条 `rule_id`**。三者缺一，规则层就只是装饰。

trace 里因此多了 `rule_id` / `rules_matched` / `rules_violated`，
详见 [`examples/README.md`](examples/README.md)。

---

## Conditional Immune Response (条件触发的免疫响应)

前两节解决的是"**判得准**"，这一节解决"**判得起**"。

全量见证是正确但昂贵的：一次 git 变更集要 snapshot 加 diff，一次内容校验要读整个文件。
如果一次无关紧要的追加和一次覆盖核心配置付一样的代价，那这条防线在长会话里迟早会被
关掉——**而"关掉"和"从来没有"是同一个结果**。

所以按生物学分两级：

| | Tier 1 先天免疫 | Tier 2 适应性免疫 |
|:--|:--|:--|
| 触发 | 规则声明 `risk_level: low`（省略即视为 low），且系统未发炎 | `risk_level: high`、规则要求见证、或系统已发炎 |
| 见证 | 存在性 + mtime（命令前后各一次 `stat`） | git 变更集 + 内容读回 |
| 开销 | 无 git 进程，不读文件 | 一次 snapshot、一次 diff、一次读文件 |
| 能看见 | "它到底动没动" | "动的是不是只有它" |
| 看不见 | 顺手多改的第三个文件 | —— |

### 炎症风暴

一个刚被证伪的系统，不该继续相信廉价检查。所以判词会累积：

- **连续 2 次 `FAIL`**，或**1 次 `INVALID`** → 进入 `INFLAMED`；
- 发炎期间**所有调用**强制走 Tier 2，包括它自己的策略声明为 low 的调用；
- 需要**连续 5 次 `PASS`** 才恢复 `NORMAL`。

`INVALID` 的门槛更低，因为它意味着"没看见"——一个看不见的防线已经在失效了。

**愈合刻意比受伤慢。** 两次失败点火，五次通过才熄灭；恢复期内再失败一次，进度归零。
对称的响应挡不住任何东西。

### 炎症不会因为重启而消失

只在内存里记录的炎症，是一个**越狱通道**：崩掉 sidecar、重启，嫌疑归零。而"能让 sidecar
崩溃"恰恰是被审 Agent 能做到的事。所以炎症会落盘。

```
<trace 目录>/.immune_state.json
{"version": 1, "state": "INFLAMED", "danger_count": 2,
 "recovery_progress": 0, "recovery_passes": 5, ...}
```

- 每次判词后**原子写入**（同目录临时文件 + `fsync` + `os.replace`），所以崩溃不会留下
  半截文件——半截文件恰恰会丢掉正在记录的那次炎症。
- 启动时读回。**缺失** = 正常首跑；**损坏 / 版本未知 / 字段非法** = 记 `ERROR`
  （日志里叫 *amnesia event*），因为悄悄遗忘与"被弄遗忘"在外部完全一样。
- **一切歧义都朝怀疑的方向取整。** `INFLAMED` 但危险计数为 0 → 仍然发炎；
  恢复进度超出阈值 → 压到阈值前一步。一份被篡改的记录可以让系统**更**警惕，
  不能让它更放松。

```bash
# 默认：就放在 trace 旁边
harness-audit-server --cwd /path/to/repo

# 放到 Agent 写不到的地方（推荐：被审进程能写状态文件就能删它）
harness-audit-server --cwd /path/to/repo --immune-state /var/lib/audit/immune.json

# 显式要求失忆——这是唯一的失忆入口
harness-audit-server --cwd /path/to/repo --immune-state none
```

> **残余风险：** 文件是普通 JSON，没有签名。能写该路径的 Agent 仍可删改它。
> 抗篡改需要放到被审进程写不到的位置，或加 HMAC——MVP 未做。
> `test_inflammation_survives_a_restart` 证明的只是"重启不再自动洗清"，
> 不是"无法伪造"。

### 真实的 trace：同一策略，两种代价

[`examples/sample_trace.jsonl`](examples/sample_trace.jsonl) 的 id=5..7 是同一次会话：

| id | 调用 | tier | `immune_state` | 判词 |
|--:|:--|:--|:--|:--|
| 5 | `write_file missing_dir/target.py` | tier2（声明为 high） | NORMAL | **FAIL** |
| 6 | `append_file no_such_dir/log.txt` | tier1（声明为 low） | **INFLAMED** | **FAIL** |
| 7 | `append_file log.txt` | **tier2**（同一条 low 规则） | INFLAMED | **PASS** |

id=7 和 id=6 是同一条规则、同一个工具，代价却不同：**发炎把低风险调用也提升到了全量
校验**。这就是 `--rules` 之外的第二个调度器——一个由历史而不是由声明驱动的调度器。

### 不放松，只是看得少

这是硬约束：**分级只决定"采集哪些见证"，不决定"见证意味着什么"**。

- Tier 1 的 `PASS` 是**在更弱证据上的 PASS**，不是在相反证据上的 PASS：
  报告成功但文件没有变化，Tier 1 照样判 `FAIL`（`test_innate_tier_still_catches_false_completion`）。
- 规则要求 `git_diff` 见证 → 自动升级 Tier 2，因为 stat 满足不了它；
  硬塞进 Tier 1 只会让它变成永久的 `INVALID`。
- **没有任何规则的工具一律 Tier 2**：**"没声明"不等于"低风险"**，
  否则加一个 `rules/` 目录反而会让那些没被提到的工具变弱。
- Tier 1 的 `PASS` 在 `reason` 里带 `[innate tier: existence and mtime only, no git change set]`，
  trace 里记 `tier` / `witness_scope` / `witness_mtime`。

### 已知盲点（写进代码，而不是写进脚注）

Tier 1 看不见"顺手多改了一个文件"。`test_adaptive_tier_sees_drift_the_innate_tier_cannot`
把这个盲点写成了断言：同一个工具，声明 low 时判 `PASS`，声明 high 时判 `FAIL`。

这也正是 `write_file` 随包声明为 `high` 的理由：覆盖写是唯一一种"内容看起来对、副作用
却可以完全在别处"的操作；而且**同长度、同一文件系统时间戳刻度内的重写**对 mtime 不可见。
把它交给廉价层，等于用省下的几毫秒换一个看不见的漏洞。

### 怎么用

策略即代码，改一行就换档：

```yaml
rule_id: "append_landed"
target_tool: "append_file"
risk_level: "low"      # 改成 "high" 就是一行 diff，会进入评审
expectation: "file_modified"
```

```python
from audit_sidecar import ImmuneState, MCPProxy

proxy = MCPProxy(cwd=repo, rules_dir="rules", immune_state=ImmuneState())
proxy.immune_state.is_inflamed()      # 当前是否处于炎症状态
proxy.immune_state.snapshot()         # {"state": ..., "danger_count": ..., ...}
```

阈值可调（`ImmuneState(fail_threshold=3, recovery_passes=6)`），但
`recovery_passes < fail_threshold` 会被拒绝——愈合比受伤快，是配置错误而不是选择。

---

## 目录结构

```
agent-harness-audit/
├── README.md
├── audit_sidecar/
│   ├── __init__.py       公开接口
│   ├── verdict.py        四值判词引擎
│   ├── confidence.py     信任能荷账户（滑动窗口）
│   ├── judgment_base.py  外置判据库（加载 rules/*.yaml）
│   ├── rule_engine.py    规则判词（在四值格之上收紧）
│   ├── immune_state.py   免疫状态机（炎症 / 恢复 / 落盘）+ 分级路由（tier1 / tier2）
│   ├── runner.py         见证采集（真实执行 + 独立读回；full / stat 两级）
│   ├── git_witness.py    Git 见证（真实变更集）
│   ├── mcp_proxy.py      MCP 拦截（协议层声明 vs 物理效果）
│   ├── mcp_server.py     stdio JSON-RPC 服务端（console script）
│   ├── self_audit.py     自审计（mode 4 防线）
│   ├── cli.py            命令行入口
│   └── rules/
│       ├── write_file.yaml  risk_level: high（覆盖写必走全量）
│       └── append_file.yaml risk_level: low（追加走先天免疫）
├── pyproject.toml        打包配置（运行时仅需 PyYAML）
├── .gitignore
├── examples/
│   ├── sample_trace.jsonl   真实审计留痕（Agent 收到 ok / 账本记 FAIL / 炎症升级）
│   └── README.md            样例说明
└── tests/
    ├── test_verdict.py
    ├── test_confidence.py
    ├── test_judgment_base.py
    ├── test_rule_engine.py
    ├── test_immune.py        分级免疫（tier 路由 / 炎症 / 恢复 / 重启后仍发炎）
    ├── test_runner.py
    ├── test_git_witness.py
    ├── test_mcp_proxy.py
    └── test_e2e_stdio.py   端到端（真子进程 + 真管道）
```

---

## 快速开始

```bash
# 1. 安装（会自动带上 PyYAML；默认规则已包含在包里）
pip install dist/agent_harness_audit-*.whl    # 或 python3 -m pip wheel . --no-deps -w dist/

# 2. 直接跑，不需要任何路径参数
harness-audit-server --cwd /path/to/project
```

**The package ships with a default rule `no_file_drift` out of the box.**
装完即生效：`write_file` 必须同时有文件见证与 git 变更集，否则判 `INVALID`。
随包另有一条 `append_landed`（`risk_level: low`），演示先天免疫：追加只做存在性 +
mtime 校验。想用自己的策略，把 `.yaml` 放进工作目录的 `rules/`，或用 `$HARNESS_AUDIT_RULES`
指向任意目录；两者都会覆盖随包默认值。

> 在**非 git 目录**里运行时，git 见证无法采集，判词为 `INVALID`（`reason` 写明原因），
> 而不是崩溃——"没有看"不等于"看过了并且通过"。此时可用 `--repo` 指向一个仓库，
> 或用 `--rules none` 显式接受纯四值格。

源码开发：

```bash
cd agent-harness-audit
pip install -e .

python3 -m unittest discover -s tests -p 'test_*.py'   # 348 项（须在仓库根目录运行）
python3 -m audit_sidecar.cli                # demo，先跑自审计，不通过则拒绝启动

harness-audit-server --cwd /path/to/project # 无需 --rules
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

带判据库的判词（详见下文「外置判据库」）：

```python
from audit_sidecar import JudgmentBase, MCPProxy, RuleEngine

engine = RuleEngine(JudgmentBase("rules"))
decision = engine.judge(
    "write_file",
    intent=True,
    effect=True,                     # 文件见证：写入确实落地了
    check=True,                      # 工具层对 agent 说成功
    witnesses={"file", "agent_report"},  # 但没有 git 变更集
)
print(decision.verdict, decision.rule_id)   # INVALID no_file_drift

# 接入拦截层后，同一判断会自动发生，并写进 trace
proxy = MCPProxy(cwd="/path/to/repo", rules_dir="rules")
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
| `--immune-state` | `.immune_state.json`（与 `--trace` 同目录） | 炎症计数的持久化文件；`none` 表示只在内存（重启即遗忘，唯一的失忆入口） |
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
5. **炎症计数默认写在 trace 旁边**（`.immune_state.json`），重启后继续生效。若被审 Agent
   对该目录有写权限，它就能删除或篡改这个文件——这时把 `--immune-state` 指到 agent
   写不到的位置（见[炎症不会因为重启而消失](#炎症不会因为重启而消失)）。

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
python3 -m unittest discover -s tests -p 'test_*.py' -v
# Ran 348 tests ... OK
```

> **必须在仓库根目录运行。** 起始目录写的是相对路径 `tests`；换到其他目录执行时
> unittest 会静默地只收集到一部分模块（实测从 `/tmp` 运行只跑 31 项并报 9 个 error），
> 而不是干脆地失败——一个少跑了 317 项却"通过"的测试，比一个红掉的测试更危险。
> 因此 `-s` 与 `-p` 都显式写出：`-p 'test_*.py'` 排除 `tests/` 下将来可能出现的
> 辅助模块被误当作测试收集。

覆盖：

- **判词**：四种判词各自的触发场景（重点 `INVALID` 不许变 `PASS`、`FAIL` 的假完成/假失败/
  意图未实现三种分支）、判词优先级、`check` 归一化与拒绝、keyword-only 约束。
- **免疫分级**：`ImmuneState` 的阈值/连续计数/`NOT RUN` 不计入/恢复期归零/参数校验；
  `select_tier` 的整张判定表（无规则、声明 low、省略字段、声明 high、规则要求见证、
  已发炎、无可观测路径）以及三个参数的类型校验；`stat` 见证的观测与拒绝
  （`expected_content` 不得在廉价层被静默丢弃）；tier 路由在**真实仓库**里的四种组合；
  发炎后同一条 low 规则确实升级到 git 变更集；五次通过恢复；`INVALID` 一次即发炎；
  注入的 `ImmuneState` 与调用方共享；响应不泄漏 tier。
- **炎症持久化**：发炎 / 恢复到一半 / 已痊愈三种状态各自跨实例还原；缺失文件不创建、
  也不写盘；`NOT RUN` 不重写文件；损坏 JSON、未知版本、未知状态、负计数各自记 ERROR 并回退；
  伪造的恢复计数被压到阈值前一步（不能跳过等待）；`INFLAMED` 配 0 危险计数仍保持发炎；
  原子写入不留临时文件；父路径不可写时只记日志不抛异常；`reset()` 持久化；
  `--immune-state none` 与显式路径。
- **重启**：真起两个 `python -m audit_sidecar.mcp_server` 子进程，第一个退出后启动第二个，
  由后者从磁盘读回前者的状态。验证炎症跨进程存活、痊愈跨进程存活、`--immune-state none`
  如实退回失忆、显式路径优先生效。**这是"重启不能洗白"唯一的实证。**
- **自身不产生漂移**：状态文件就在被审仓库里，而 git 见证会列未跟踪文件——
  连续两次 `write_file` 必须都是 PASS 且 `unexpected_changes` 为空，
  否则审计会把自己的笔记本当作罪证。
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
- **见证只有文件系统与 git**。没有进程退出码族，没有网络副作用；
  `git` 见证依赖工作树状态，对已 commit 的历史改动不可见（基线是工作区快照）。
- **Tier 1 有明确盲点。** 先天免疫只看存在性与 mtime，**看不见"顺手多改了一个文件"**；
  同长度、同一文件系统时间戳刻度内的重写对它完全不可见。这不是缺陷而是取舍，
  所以覆盖写在 `write_file` 的 `risk_level: "high"` 里，并把盲点写成了一条测试
  （`test_adaptive_tier_sees_drift_the_innate_tier_cannot`）。
- **炎症状态跨进程持久化（v0.7.0 起）。** `harness-audit-server` 默认把计数写在
  `--trace` 旁边的 `.immune_state.json`，每次判词后原子写入，启动时读回。
  **杀进程重启不再能洗清嫌疑**——这正是它存在的理由：能安排崩溃的是被审的 Agent 自己。
  用 `--immune-state none` 显式要求失忆（这是唯一的失忆入口），或用 `--immune-state PATH`
  把文件放到被审目录之外。

  **残余风险（不隐藏）：** 状态文件是普通 JSON，没有签名。一个**有权写入该路径**的 Agent
  仍然可以删掉或改掉它；损坏或缺失时系统回退 `NORMAL` 并在 stderr 记 ERROR
  （日志里写作 amnesia event）。要做到抗篡改，需要把文件放到 Agent 写不到的地方，
  或加 HMAC——MVP 未做。**这比"重启即遗忘"强得多，但不是不可伪造的。**
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
- **规则层是 opt-in 的。** `MCPProxy` 不传 `rules_dir` 就是纯四值格；服务端才自动探测
  `./rules`。不给规则不会静默改变老调用的判词。
- **`rules/` 随包发布（v0.6.1 起）。** 默认规则在 `audit_sidecar/rules/` 内，
  作为 package data 打进 wheel，所以 `pip install` 之后**不需要任何配置**即可生效。
  解析顺序是 `$HARNESS_AUDIT_RULES` → 当前目录 `./rules` → 包内 `rules/`，
  前两者用于覆盖默认策略。`$HARNESS_AUDIT_RULES` 指向不存在的目录会**报错**，
  而不是悄悄退回默认策略——否则操作者会以为自己的规则在跑。
- **规则只覆盖 `write_file` 与 `append_file`。** 随包带两条：`no_file_drift`
  （`risk_level: high`）与 `append_landed`（`risk_level: low`）；`run_command`
  没有规则，直接走格子并默认 Tier 2。规则表达力也有限：只有三个
  `witness_type`、一个 `expectation: file_modified` 和两个 `risk_level`，
  写成别的一律加载报错——宁可拒绝，也不静默忽略。
- **非 git 目录下只有文件见证。** 需要变更集的工具（`write_file`）在非 git 目录
  判 `INVALID`：不是"通过"，是"没看到"。要么 `--repo` 指向仓库，要么
  `--rules none` 明示接受较弱策略。
- **规则不做跨工具推理。** 每条规则独立判定，没有"先 A 后 B"的顺序约束，也没有
  规则之间的依赖。
- **无持久化。** 账户与审计日志都在内存里，进程退出即丢（trace 文件除外）。

## 路线图

1. **见证采集**：
   - 文件系统：已具备（存在性 + 内容子串）。
   - Git 变更集：已具备（`git_witness.py`）。
   - 待扩展：内容哈希、进程退出码族、网络副作用。
   - mtime：已用于 Tier 1（存在性 + mtime，见[条件触发的免疫响应](#conditional-immune-response-条件触发的免疫响应)），
     精度处理与内容哈希仍未做。
2. **MCP 拦截**（语义与传输已具备，握手未接）：
   - 已具备：`MCPProxy` 的调用边界、被动响应、工具注册表、注入防护；
     `mcp_server.py` 的 stdio JSON-RPC 服务端与 JSONL 审计留痕。
   - 待扩展：从协议帧的 `result.isError` 取声明而非由客户端上送、`initialize` 握手与
     `tools/list`、Content-Length 分帧、转发到外部 MCP server、并发与批处理。
3. **外置判据库**（已具备基础形态）：
   - 已具备：`rules/*.yaml` 加载与严格校验、按工具选规则、单调收紧、`rule_id` 入 trace、
     规则层自审计。
   - 待扩展：更多 `expectation`（`file_created` / `content_matches` / `no_unexpected_changes`）、
     规则继承与分组、按路径作用域（`target_path` glob）、规则版本与生效日期、
     远程判据库与签名。
4. 账户持久化与跨会话聚合；把判词流导出为可复核的审计日志。

## 许可

代码 MIT；论文文本 CC BY 4.0。
