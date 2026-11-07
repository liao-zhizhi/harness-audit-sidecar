# 因果接地加固报告：从「盲信布尔」到「可证伪承诺」

**日期**：2026-11-07
**改动**：`agent-metabolism-os/product_mvp/cel_engine.py`、`api_server.py`、`static/app.js`、`example_client.py`、`tests/`
**新增**：`agent-harness-audit/audit_sidecar/cloud_report.py`、`examples/real_causal_e2e.py`
**验证**：服务端 65 tests OK；Sidecar 588 tests OK

---

## 0. 一句话结论

**漏洞已堵，因果仍未接地。**

云端现在会拒绝「声称成功但拿不出摘要」的请求，并且拒绝重放。
但它**依然无法验证摘要描述的东西真的发生过**——因为服务器碰不到客户端的磁盘。

场景 C 把这个残余漏洞**断言**下来了，而不是绕过去。

---

## 1. 修了什么：代码逻辑

### 1.1 废除失效的服务器端哈希（这是本次的核心）

原代码：

```python
if recorded_artefact and os.path.isfile(recorded_artefact):
    recorded_artefact = "{}#{}".format(
        recorded_artefact, digest_file(recorded_artefact))
```

`os.path.isfile()` 在**服务器自己的文件系统**上解析客户端传来的路径。
远程客户端传 `"hello.txt"`，服务器上没有这个文件 → 分支永不进入 →
`artefact` 落库时只是一个裸字符串。

**一个结构性永不执行的校验，比没有校验更危险**，因为它读起来像"已覆盖"。

现在这段代码被**删除**，而不是修补。服务器没有立场去读客户端的磁盘。

### 1.2 成功声明必须携带摘要

```python
_SHA256_BARE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA256_PREFIXED = re.compile(r"^sha256:([0-9a-fA-F]{64})$")

def normalise_witness_hash(value):
    """裸 64 hex 或 sha256:<64 hex>，其余一律 None。"""
```

在 `CelGateway.verify()` 中：

| 条件 | 结果 |
|:--|:--|
| `witness=True` 且无合法摘要 | `UnwitnessedClaim` → **422, INVALID, ledger_written=false** |
| `witness=True` 且无 `trace_id` | 同上 → **422** |
| `witness=True` 且摘要格式非法 | `VerifyError` → **422** |
| `witness=True` 且 `trace_id` 重复 | `VerifyError` → **409**（重放） |
| `witness=False`（诚实失败） | **正常记账，不要求摘要** |

**最后一行是刻意的，且不可省的。** 失败的实验没有产物可哈希。
若强制要求摘要，唯一的理性策略就是"干脆不上报失败"——那正是上一轮
我们花了整节论证要消灭的"生产沉默"。

### 1.3 摘要进入哈希链

```python
receipt["witness_hash"] = digest or ""
receipt["trace_id"] = trace
sealed = self._append(receipt)
if trace:
    self._trace_ids[trace] = agent_id.strip()
```

`_public()` 与 `stats()` 同步暴露新字段；`grounded` 的语义从
"artefact 字符串非空"改为 **"存在合法摘要"**——从描述打字，改为描述证据。

### 1.4 被拒绝的提交单独计数

```python
"rejected_unwitnessed": len(self._rejections),
```

它**不在链上**（按构造就没有条目可链），所以是 `stats()` 里唯一一个
"保留值"而非"派生值"。我在代码注释里明写了这一代价：

> 读它当作烟雾报警器，不要当作证据：能重启进程的攻击者可以清掉它，
> 而且从未有任何判词被记在攻击者头上。

### 1.5 Sidecar 上报（任务 2）

`audit_sidecar/cloud_report.py`：

```python
def witness_digest(path):
    """sha256:<hex> of the artefact, or None if it cannot be read."""
```

关键约束：**摘要由 Sidecar 从磁盘上真实存在的产物算出来**，而不是从模型
的自述里抄。仅当 `result.effect_observed is True` 时才计算——
没有观测到效果就无从产生摘要。

若云端返回 `INVALID` / 非 2xx：

```python
_LOG.error("云端因果校验失败 status=%s verdict=%s: %s", ...)
```

### 1.6 大屏与示例客户端（附带修复）

`static/app.js` 的 `verified` / `underclaim` 预设原本 `witness: true` 无摘要，
加固后会被 422 拒绝——**演示会直接坏掉**。已修：

- 摘要取字面串 `"cel-dashboard-simulation"` 的 sha256（常量）
- `artefact` 改为 `simulated://...`
- `trace_id` 每次生成

**为什么这样做而不是随便给个哈希**：这是一次按钮模拟，不是真实接地。
让账本条目自己写着 `simulated://`，比事后口头解释诚实。

---

## 2. E2E 实测结果

脚本：`examples/real_causal_e2e.py`
运行：真实 `uvicorn` 子进程 + 真实 HTTP + 真实文件系统。

### 场景 A（骗子）

```json
请求: {"witness": true, "witness_hash": null}
```

```
HTTP 422
verdict: INVALID
ledger_written: False
detail: witness=true requires witness_hash: a success claim with no digest is
        not evidence the cloud can hold you to
账本: 0 -> 0 (delta 0)
```

### 场景 B（诚实者）

```
命令:   printf hello > hello.txt   (SafeRunner 真实执行)
见证:   effect_observed = True
本地摘要: sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
HTTP 200   verdict=PASS   grounded=True
账本: 0 -> 1 (delta 1)
链上条目: seq=1  witness_hash=2cf24dba…9824  (与本地摘要一致: True)
```

### 场景 C（谨慎的骗子）— 我加的

```
无任何文件，直接提交 sha256("hello") = 2cf24dba…9824
HTTP 200   verdict=PASS
账本: 1 -> 2 (delta 1)
```

**场景 C 通过，而这个通过是诚实的测试结果，不是修复的缺陷。**
它精确地标出了这次加固的边界。

### 最终状态

```
chain_ok=true  total_entries=2  rejected_unwitnessed=1  verified_passes=2
```

---

## 3. 这次修复到底买到了什么

| | 加固前 | 加固后 |
|:--|:--|:--|
| 提交形式 | `witness: true`（布尔） | 摘要 + trace_id |
| 懒骗子（交 null） | 记账 PASS | **422 拒绝** |
| 重放他人真实摘要 | 可重复记账 | **409 拒绝** |
| 事后可证伪性 | 无 | **有**：拿到真实产物即可复核摘要 |
| 谨慎骗子（编造摘要） | 记账 PASS | **仍记账 PASS** |
| 云端验证来源 | 无 | **仍无** |

**准确的表述是：从「不可证伪的布尔」变成「可证伪的承诺」。**

- 加固前：账本里写着一个永远无法被推翻的 `true`。
- 加固后：账本里写着一个具体摘要。日后任何人拿到真实产物，一比对就知道
  当初有没有撒谎，对不上就是铁证。

这**是**真实的进步（问责性），也**不是**真实性的进步（预设为真）。

---

## 4. 还剩什么没堵上，以及什么才能堵上

残余漏洞的根本原因不是代码，是协议位置：

> **服务器无法观测客户端的磁盘。** 任何客户端自算的摘要，本质上都是
> 「带格式的自述」。

**Distributed reality limit: Cloud servers cannot access client disks. Any
client-generated hash is a "falsifiable promise", not absolute grounding.
True grounding requires hardware TEEs or offline physical audits. Our ledger
records the promise, not the physical truth.**

这条边界已在公网实例上实测确认（见 §7）。要真正闭合，必须引入
**客户端无法单方面控制**的见证：

| 方案 | 机制 | 代价 |
|:--|:--|:--|
| 仪器级审计轨迹 | 数据在采集时点由仪器签名（ALCOA+ / 21 CFR 11.10(e)） | 需改造实验室 |
| 硬件根信任 | TPM/TEE 内签名，密钥不可导出 | 需专用硬件 |
| 独立公证 | 第三方对产物做时间戳/见证 | 需信任第三方 |

在接入其中之一之前，本账本里的 `PASS` 只能读作：

> **"做出了一份格式合法、可被日后推翻的承诺"**，而不是"这件事真的做了"。

这句话已写进 `cel_engine.WitnessHashLimits`，与代码放在一起，
防止下一次有人把"格式校验通过"讲成"因果已接地"。

---

## 5. 线上实例已加固（2026-11-07 14:41 部署）

本次修改已部署到 `https://demo.silicon-probe.com`。

**部署形态**：Docker `cel-app`，Cloudflare Tunnel 前置，源码在
`/opt/agent-metabolism-os`。

| 项 | 值 |
|:--|:--|
| 镜像 | `cel-dashboard:mcgs-0.4.2` |
| 回滚备份 | `/root/cel-backup-20261107-063441/`、`cel-dashboard:rollback-*` |
| 容器状态 | **healthy**（此前为 unhealthy，见 §5.1） |
| 账本 | 随容器重建清空；链上现有 2 条实测记录 |

### 5.1 顺手修掉的两个既有缺陷

| 缺陷 | 现象 | 原因 | 修复 |
|:--|:--|:--|:--|
| 健康检查永久失败 | 容器 21 小时 `unhealthy` | 镜像 `ENV PORT=7860`，但启动命令写死 `--port 8000`，健康检查读 `PORT` 探 7860 | 重建容器改用镜像默认 CMD（7860），端口映射改为 `80:7860` |
| 前端缓存穿透失败 | 后端已更新，大屏仍显示旧逻辑 | Cloudflare 缓存 `/static/*` 4 小时（`cf-cache-status: HIT`，`age: 5164`），`app.js` URL 未变 | 模板插入 `?v=<静态资源 sha256 前 10 位>`，HTML 为 `DYNAMIC` 不缓存，故改模板即生效 |

第二个缺陷值得单独记：**它是"部署成功"与"用户看到变化"之间的一个静默断点。**
API 已经返回 `witness_hash`，大屏却在渲染旧代码，看起来像修复没生效。
版本串改为由字节摘要生成（而非手工常量），因为"忘记改常量"正是这类故障的成因。

### 5.2 部署后实测

见 §7。

### 5.3 第三次修：`grounded: true` 本身在过度声称

前两个缺陷是工程问题。这个不是。

加固后服务端对每个 `PASS` 返回 `"grounded": true`，其实现是
`bool(witness_hash)` —— 即"调用方提供了格式合法的摘要"。于是场景 C 里那个
**没有创建任何文件**的骗子，也拿到了 `grounded: true`，并且在大屏上和诚实者
渲染出**完全相同**的 `⛓` 徽标与 `HEALTHY` 状态。

也就是说：后端刚拒绝完"无摘要即成功"，转手就用一个名为 `grounded` 的布尔
把"提供了摘要"讲成了"已接地"。这正是 §4 那条边界要禁止的表述，
而它是**我们自己**写进响应里的。

修正（`cel_engine.py`）：

| 字段 | 修前 | 修后 | 理由 |
|:--|:--|:--|:--|
| `grounded` | `bool(witness_hash)` | **`None`** | 服务器无法回答这个问题。`null` 是"未知"，不是"否" |
| `commitment_recorded` | — | `bool(witness_hash)` | 这才是服务器真正知道的事 |
| `grounding` | — | `"unverifiable_by_server"` | 把上界写进协议，而不是留在文档里 |

前端同步（`app.js`）：徽标从 `⛓ <hash>` 改为 **`⛓ commit <hash>`**，
tooltip 明确写出"the server cannot verify the file exists"。
`commit` 是服务器能站住的措辞，`⛓` 单独出现则暗示"已上链核验"。

一个副作用值得记下来：**真实摘要与编造摘要在屏幕上仍然无法区分**，因为
服务器确实区分不了。给它们不同的外观会伪造确定性。修完之后两者的共同读数是
"这里有一份可被日后推翻的承诺"，而不是"这里有一条已核验的事实"。

### 5.4 遗留的同类问题：`trust_score` 也容易被读成"真实性"

大屏上 `liar` 和 `honest` 都是 `[trust 100.0]`。这不是 bug：
`trust_score` 度量的是**自述与账本的一致性**，不度量**事实真实性**。
一个前后一致、从不自相矛盾的骗子，信用分会一直是满分。

区别在于，`grounded` 的误导发生在**协议字段**里，所以必须改（§5.3）；
`trust_score` 的误导发生在**读者**身上，改名会破坏既有集成，
因此只登记为已知限制：

> `trust_score` 的准确读法是"我有多没自相矛盾"，不是"我有多可信"。
> 把高分转述为"已核实"，与 §4 的 F8 属于同一类错误。

真正能拉低骗子信用分的机制是**事后证伪**：任何人拿着真实产物
（或证明其不存在）提交反证，账本上那条承诺即被推翻，信用随之扣减。
这条回路**尚未实现**——这正是场景 C 仍记为 `PASS` 的原因，
也是本系统当前最值得做的下一个功能，而不是继续加检测维度。

---

## 6. 复现

```bash
# 服务端契约测试
cd agent-metabolism-os/product_mvp && python -m unittest discover -s tests

# 端到端三场景
cd agent-harness-audit && python3 examples/real_causal_e2e.py \
    --server-python /path/to/python-with-fastapi --json /tmp/causal_e2e.json
```

---

## 7. 公网实测（`https://demo.silicon-probe.com`）

### 7.1 攻击：无摘要

```
POST /api/v1/verify
{"agent_id":"attacker","intent":"fake","check":true,"witness":true}

HTTP 422
{"verdict":"INVALID",
 "detail":"witness=true requires witness_hash: a success claim with no digest
           is not evidence the cloud can hold you to",
 "ledger_written":false}
账本 total_entries: 0 -> 0
```

### 7.2 诚实者

```
POST /api/v1/verify
{"agent_id":"honest","intent":"real","check":true,"witness":true,
 "witness_hash":"2cf24dba5f…9824","trace_id":"test-001"}

HTTP 200  verdict=PASS  grounded=null  commitment_recorded=true  seq=1
账本 total_entries: 0 -> 1
```

### 7.3 重放同一 trace_id

```
POST /api/v1/verify  {"…","trace_id":"test-001"}

HTTP 409
{"verdict":"INVALID",
 "detail":"trace_id 'test-001' was already booked by 'honest'; a claim cannot
           be replayed",
 "ledger_written":false}
账本 total_entries: 1 -> 1
```

### 7.4 模拟数据标识

```
POST {"agent_id":"robot_arm_01","artefact":"simulated://vial/42",…}

HTTP 200  verdict=PASS
链上: #002 robot_arm_01 PASS hash=d19733cb344a38d4 art=simulated://vial/42
```

### 7.5 大屏渲染（浏览器实测，非推测）

CDP 从真实页面抽取的 DOM：

```
#001 14:51:36 PASS ⛓ commit 2cf24dba…9824 honest real [trust 100.0]
#002 14:51:39 PASS ⛓ commit 4af2acf4…a5f4 liar   invented [trust 100.0]

ev-tag ev-hash  title="commitment recorded: sha256:2cf24dba5f…9824 ·
                        trace live-001 — the server cannot verify the file exists"
资源版本: /static/app.js?v=<部署后摘要>
```

**注意 #002**：`liar` 的徽标与 `honest` 完全一致，因为它提供的摘要格式合法。
这不是渲染 bug，是 §4 边界的直接可见后果——**大屏无法替你分辨骗子**，
它只能告诉你"这里记了一份承诺"。任何把 `⛓ commit` 读成"已核验"的转述都是错的。

`simulated://` 渲染路径在此前一次部署中已实测（`ev-tag ev-sim`，
琥珀色，tooltip 为完整 `simulated://vial/42`）；本次 `app.js` 只改了摘要分支，
未触及该分支，且资源版本串随字节变化而更新。

### 7.6 最终状态

```
chain_ok = True    total_entries = 2
#001 honest       PASS  2cf24dba…9824
#002 robot_arm_01 PASS  simulated://vial/42
```

---

## 8. 这次部署改变了什么，没改变什么

| | 部署前（公网） | 部署后（公网） |
|:--|:--|:--|
| 无摘要的成功声明 | 记账 PASS | **422 拒绝，账本不变** |
| 重放他人摘要 | 重复记账 | **409 拒绝** |
| 大屏是否显示证据 | 否 | **是**（`⛓ commit` / simulated） |
| `grounded` 字段 | `true`（仅表示有摘要） | **`null`**（服务器无法判定） |
| 谨慎骗子（编造摘要） | 记账 PASS | **仍记账 PASS**（§3 已定性） |
| 前端缓存 | 4 小时陈旧 | **版本串自动破缓存** |

**没有改变的是 §4 那条边界。** 场景 C 依然成立，F8 依然是能力上界。
线上加固让账本从"不可证伪的布尔"变成"可证伪的承诺"，
**没有**让它变成"物理真相的记录"。对外表述必须保持这个区分。

---

## 9. 事后证伪回路（2026-11-07 新增）

§8 的"可证伪的承诺"此前只是**形容词**：账本理论上可以被推翻，但没有任何
动作能执行这件事。一个无法执行的"可证伪"，与"不可证伪"在工程上没有区别。

现已实现 `POST /api/v1/falsify`，把承诺的**可证伪性**变成动词。

### 9.1 语义

| 动作 | 结果 |
|:--|:--|
| `POST /api/v1/falsify {trace_id, reason, evidence}` | 追加 `REFUTED` 条目，目标 Agent 信任分清零 |
| 原 PASS 条目 | **保留 `PASS` 与原始 `chain_hash`**，附加 `refuted: true` 与证伪指针 |
| `GET /api/v1/ledger` | 每条 PASS 带 `refuted` 标志，链上仍 `chain_ok: true` |
| `GET /api/v1/stats` | `refuted_commitments` / `standing_passes`（= passes − refuted） |

**为什么不改写原条目**：链哈希覆盖条目正文。把 `PASS` 原地改成 `REFUTED`
会让 `verify_chain()` 失败——**用"纠正记录"的方式摧毁记录**。所以证伪是
**追加**一条指向 `refutes_seq` 的证据，历史与当前判断同时存在于链上。
这也是审计应有的样子：既看到系统当时说了 PASS，也看到后来它被推翻。

### 9.2 拒绝路径（都有测试）

| 状态 | 条件 | 理由 |
|:--|:--|:--|
| `404` | `trace_id` 无对应 PASS | 不能凭空证伪一段不存在的历史 |
| `409` | 已被证伪 | 第二个观察者没有新证据，不重复计价 |
| `409` | 条目无摘要（诚实失败） | 没有承诺可推翻；推翻真话不是证伪 |
| `422` | `hash_mismatch` 缺 `observed_hash` | 发现不完整 |
| `422` | `observed_hash` 等于承诺摘要 | 那是**确认**，不是证伪 |
| `422` | `evidence` 为空 | 空依据只是断言，无法审计 |
| `403` | 缺写权限 | 匿名证伪 = 一行请求毁掉任意 Agent 的后门 |

### 9.3 R2：上行是墙，不是坡

证伪不只扣分，还**关闭了自动恢复**：

- `PenaltyLedger.record_falsification()` → trust **归零**，
  `REFUTATION_PENALTY = 100`（越过 `CRITICAL`）。
- `record_success()` 对已证伪账本**不再加分**。测过：连续 200 次干净
  PASS 后信任仍是 `0.0`。
- `next_charges_until("HEALTHY")` 直接抛 `PenaltyError("...unreachable...")`，
  而不是给一个"再跑 50 次就洗白"的数字。给数字就是**卖假安慰**。

若上行只是陡峭，撒谎成本就变成"事后继续工作多久"的函数——奖励了账本最该
惩罚的持续性。

### 9.4 大屏（浏览器实测）

对账按钮只在**可证伪**的行上出现（`PASS` + 有摘要 + 有 trace + 未被证伪）。
弹窗要求选择 `file_not_found` / `hash_mismatch`，并强制填写依据。
CDP 从真实页面抽取的提交后 DOM：

```
#001 16:02:27 PASS    ✖ refuted        assay_bot  seal vial 42
                                       [refuted → trust 100.0 at seal]
#002 16:03:28 REFUTED 🎯 refutes #1    assay_bot  falsification of entry 1
                                       [file_not_found · trust 0.0]

指标: PASS 1 · FAIL 0 · INVALID 0 · 已证伪 1
平均信任分: 0.0        健康: CRITICAL
对账按钮数: 0（已证伪的行不再提供）
```

`#001` 保留 `PASS` 字样并加删除线，**不**改成 `REFUTED`：它当时的结论就是
PASS，这一点本身要被审计。`#002` 是证伪记录，指向 `refutes_seq: 1`。

### 9.5 这条回路自己的上界（重要）

证伪是**反方向的另一个不可验证断言**。观察者说"文件不存在"，服务器同样
无法核实。区别只有两个，但都是实质的：

- **可归因** —— 证伪带观察者与 `evidence` 原文入链，一条诬告本身在账上。
- **要求举证** —— `evidence` 必填，空依据不构成发现。

因此它需要写权限。"任何人都能证伪"的准确读法是
**"任何持有审计密钥的人都能证伪"**。不做匿名版本，否则修一个谎的工具会变成
毁掉任意 Agent 的工具。

**证伪回路让账本可以被推翻，但不代表账本知道真相。** F8 依然成立 ——
只是从一条不可执行的声明，变成了一条有执行路径的声明。
