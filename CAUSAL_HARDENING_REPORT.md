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

要真正闭合，必须引入**客户端无法单方面控制**的见证：

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

## 5. 重要限制：线上实例**尚未**加固

本次修改的是**本地源码** `product_mvp/`。`https://demo.silicon-probe.com`
是已部署实例，**不会**因本次提交而改变，行为仍是旧协议。

在重新部署并复跑本报告第 2 节之前：

- 不得对外声称"云端已强制因果接地"
- 大屏那 3 条历史记录仍是模拟按钮数据（`robot_arm_01`）

---

## 6. 复现

```bash
# 服务端契约测试
cd agent-metabolism-os/product_mvp && python -m unittest discover -s tests

# 端到端三场景
cd agent-harness-audit && python3 examples/real_causal_e2e.py \
    --server-python /path/to/python-with-fastapi --json /tmp/causal_e2e.json
```
