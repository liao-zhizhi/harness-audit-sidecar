# 控制-审计并行：定位与证伪报告

只读核心实现。`experiments/parallel_audit_probe.py` 的三种时序是**模拟**；`production` 行才是当前 `JsonRpcServer._execute`。模拟跑通 ≠ 生产已并行。

场景：`write_file fake_dir/test.txt`（父目录不存在）。探针一次运行（tmp `/tmp/parallel-audit-probe-43ijca8s`）。

## 1. 定位表

| 环节 | 文件:行号 | 当前时机 | P1/P2/P3 |
|---|---|---|---|
| `write_file` 拦截入口 | `mcp_proxy.py:128-144` 组命令；`mcp_proxy.py:583-682` `intercept_tool_call` | 调用当时，shell **前** | P1 入口在 |
| `run_command` 拦截入口 | `mcp_proxy.py:167-182`；同上 `intercept_tool_call` | 同上；`expected_files_modified` 在此进入 runner | P1 入口在 |
| MCP `tools/call` → 执行 | `mcp_server.py:567-569` `_handle_tools_call`；`mcp_server.py:772-818` `_execute` | 单次请求内串行 | 无并行通道 |
| Git 基线 | `runner.py:312-339` `GitWitness.snapshot()`；`git_witness.py:168` | **执行前**一次 | P1 部分：有快照；P3 否：不与 stat 并行 |
| Stat 基线 (mtime/size) | `runner.py:357-363` `_stat_snapshot`；`runner.py:755-774` | **执行前**一次，仅 innate / git fallback | P1 部分：文件路径，不是父目录 |
| 命令执行 | `runner.py:365-367` `_execute` | 快照之后 | — |
| 事后比对 | `runner.py:398-455` `_stat_changes` 或 `changed_since` + 内容读 | **执行后**，同进程即时，不扫 jsonl | P1 比对在；P3 否：git 与 stat 互斥串行 |
| 父目录是否存在 | 无独立检查。`_stat_one` 对缺文件返回 `None`，与缺父目录不可分 | 无执行前警告 | P1 父目录：**不满足** |
| 回包对象生成 | `mcp_proxy.py:703-705` `_record` → `_tool_response`；FAIL 可带 `__audit_warning__`（743-767） | 判定之后、写盘之前 | P2：对象先于 faults |
| `audit_trace.jsonl` | `mcp_server.py:292-305` `TraceWriter.write`（fsync） | 回包对象已有、stdout 未写 | — |
| `physical_faults.jsonl` | `mcp_server.py:307-318` `_offer_memory` → `memory_bus.py:56-93` | **同一次 `_execute`**，trace 之后、`return response` 之前 | P2：**不是**日志轮询；**也不是**线程并行 |
| stdout 回包 | `mcp_server.py:537-542` `serve` → `_write` 820-826 | faults 已落盘之后 | P2：发出晚于写入 |
| 分层并行 | 无。full 要 git 则走 git；失败则 `stat_scope=True` 改走 stat（`runner.py:340-355`） | 串行 fallback | P3 **证伪** |

没有第二进程、没有 MCP notification 补慢结果。

## 2. 三种模拟 + 生产时序

| 模式 | 到 FAIL 可读 | 调用总长 | faults 相对回包 | 预检 |
|---|---|---|---|---|
| 1 事后（模拟，**非**生产） | 2.208 ms | 2.362 ms | 先写 faults，再 `reply_ready` | 无 |
| 2 并行（模拟） | 2.075 ms | 2.205 ms | 同上；事件含 `pre_snapshot` | 快照有父目录布尔，无警告字段 |
| 3 预检（模拟） | 2.053 ms | 2.144 ms | 同上 | **是**：`precheck_warning` 在 `execute_done` 前；回包含 `__precheck_warning__` |
| production（真 `_execute`） | 16.071 ms | 16.153 ms | `trace_write_return` 在 `execute_returned` 前；1 行 physical_faults | **否**：无 `__precheck_warning__`；有事后 `__audit_warning__` |

生产事件序：`call_start` → `pre_parent_stat`（探针自己的 stat）→ `trace_write_enter` → `trace_write_return`（内含 `_offer_memory`）→ `execute_returned`。

生产判定：`FAIL`，`witness_scope=stat`（无 git 则 fallback），`missing_expected=["fake_dir/test.txt"]`，`effect_observed=false`。父目录在调用前不存在，实现未把这一点送进回包。

三种模拟的毫秒差没有信息量（都是立刻失败的 `printf`）。有信息的是**顺序**和**生产是否已含执行前快照**。

## 3. 判定

### P1 — 部分成立

MCP 协议**不阻止**执行前快照：当前 sidecar **自己**跑 shell，`intercept_tool_call` 在 `_execute(command)` 之前。`runner.py` 已经对声明路径做 git fingerprint 或 `(mtime_ns, size)` 基线，执行后即时比对，不扫 `audit_trace.jsonl`。

未成立的部分：父目录存在性不是一等公民；缺父目录被折叠成“文件不存在 / untouched”。没有执行前 `__precheck_warning__`。所以不是“协议不可行”，是“快照粒度停在目标路径，没有预检警告通道”。

**不证伪。** 若审计器只包已经执行完的外部工具，执行前快照才会在协议上不可行。那不是今天的结构。

### P2 — 部分成立

`physical_faults.jsonl` 与 MCP 回包在**同一条调用栈**上：`intercept` 判定 → `_record` 组回包 → `TraceWriter.write` → `_offer_memory` → `return` → `serve` 写 stdout。探针里 production 的 `trace_write_return` 早于 `execute_returned`，且 faults 行数为 1。没有“一轮结束后扫日志”。

未成立的部分：“并行”若指与回包生成重叠的另一线程/另一时刻：没有。回包 dict 先组成，faults 后写，stdout 最后发。顺序提交，不是并行回路。

瓶颈：单线程 `_execute`；fsync trace 还挡在 faults 前面。不是 IPC、不是轮询。

### P3 — 证伪

快速 stat 与慢速 git/内容哈希不能并行触发。代码是：能 git 就 git（执行前 snapshot + 执行后 `changed_since` + 可选内容读）；不能 git 就整段改成 stat。没有“stat 先出结果、git 再补”的第二条回包或 notification。

分层并行要改协议或进程模型：一次 `tools/call` 只有一个 JSON-RPC result。要先快后慢，得加 `notifications/*`、第二次 call，或进程内 future（客户端仍只见最终一包）。当前不支持。

### 总判词

**部分成立。**

控制指令的并行副产品：在**同一进程、同一次 tools/call** 里，已经有执行前基线 + 执行后比对 + FAIL 即时写入 memory bus。不是生物意义上的并行回路，也不是事后扫日志。P3 明确证伪。改造应收窄为「预检警告 + 已有事后即时比对」，而不是上真正的执行前并行架构。

## 4. 最小改造点（只报告，未改代码）

1. **预检警告（P1 缺口）**  
   `runner.py` `_stat_snapshot` / `_stat_targets`：对每个声明路径再 `stat`/`exists` 其 `dirname`。缺父目录时在 `ExecutionResult.reason` 或新可选字段记下。  
   `mcp_proxy.py` `_tool_response`：对称于已有 `__audit_warning__`，增加可选 `__precheck_warning__`。MCP 对 extra 字段是宽松的（`__audit_warning__` 已在用）。**不必**改 JSON-RPC 方法集。

2. **P2**  
   不必为“发出前写入”再改：已经在 stdout 前完成。若要“组包前写入”，把 `_offer_memory` 挪到 `_record` 内、先于 `_tool_response`——收益小，仍是串行。

3. **P3**  
   不要在现协议里做并行 git+stat。需要并行再单独立项：进程内线程 + 仍只返回最终包，或 MCP notification。本轮证伪，不建议做。

约束核对：未改 `mcp_server.py` / `mcp_proxy.py` / `memory_bus.py`；未推远端。
