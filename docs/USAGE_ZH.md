# 因果审计 Harness Audit · 小白使用指南

> 适用版本：**0.11.7**（扩展 ID `liao-zhizhi.harness-audit`）
> 这份文档写给第一次用它的人。不需要懂 MCP、不需要懂 git、不需要读那 2000 行 README。

---

## 0. 它到底在干什么（30 秒版）

一句话：**它不评价代码好不好，只核对「AI 说做完了」和「磁盘上真的变了」是否一致。**

- AI 说「我改好了」→ 它去查文件、查 git diff、查命令退出码
- 对不上 → 判 `FAIL`，并在面板上写明「哪句话是假的」

它**不负责**：代码质量、测试生成、安全扫描、性能分析、MCP 配置检查。

---

## 1. 安装（三步，5 分钟）

### 第 1 步：装插件

```bash
cursor --install-extension /home/liaozhizhi/Gradia/agent-harness-audit/vscode-extension/harness-audit-0.11.7.vsix
```

装完重启 Cursor。

### 第 2 步：打开你要审计的项目文件夹

**先打开文件夹，再开启。** 顺序反了会绑错目录（原因见 5.2）。

### 第 3 步：点「一键开启因果审计」

左侧活动栏最下面会出现一个**盾牌图标**，点开：

1. 面板里有个绿色大按钮 **「一键开启因果审计」**
2. 点它
3. 弹窗提示「请重启窗口让 Cursor 加载 MCP」→ 点 **「重启窗口」**

它会替你做两件事：

| 做什么 | 具体 |
|---|---|
| 写 MCP 配置 | 往 `~/.cursor/mcp.json` 写入 `harness-audit` 条目 |
| 绑定工作目录 | 把 `--cwd` 和 `--trace` 绑成**你当前打开的那个文件夹** |

不用手写 JSON，不用 pipx，不用改项目仓库。

---

## 2. 先确认它真的在工作

重启后，项目根目录应该出现一个文件：

```
<你的项目>/audit_trace.jsonl
```

让它产生第一条记录：让 AI 随便写个文件，然后看 `audit_trace.jsonl` 是不是多了一行。

- **多了** → 正常工作
- **没有 / 空的** → 去看第 5.1 节

---

## 3. 怎么看结果：四个值

面板上每条记录带一个徽章。**只有四个值，没有第五个。**

| 徽章 | 含义 | 人话 |
|---|---|---|
| 【PASS】 | 世界与意图一致 | 对得上 |
| 【FAIL】 | 我看了，我看到失败 | **抓到了假完成** |
| 【INVALID】 | 我没看，所以拒绝判 | 缺证据，不是坏了 |
| 【NOT_RUN】 | 什么都没跑 | 没发生 |

### 3.1 两个反直觉的地方（重要）

**① `PASS` 不等于「AI 干活了」。**

如果文件**本来就已经是对的**，AI 什么都不做，也会判 `PASS`。因为它只能报告「世界现在符合意图」，不能报告「这是谁干出来的」。别把 PASS 读成「这个 agent 很能干」。

**② `INVALID` 不等于「出 bug 了」。**

它是审计器**主动拒绝判决**：没有任何独立证据能核对那句话。常见原因：

- 项目不是 git 仓库 → 只能比对文件存在性和修改时间（`witness_scope: stat`），全量对账（`full`）不可用
- 没有基线可比 → 需要先跑 `harness-audit baseline`
- 审计器自己过载 → 它宁愿拒判，也不肯吐一个假结论

看到 `INVALID` 的正确反应是「**补证据**」，不是「重跑直到变 PASS」。

### 3.2 FAIL 时面板会多给你三行

```
原因: false completion: report says success, witness says no effect
没动的文件: src/main.py
多动的文件: （无）
```

「没动的文件」= 它声称改了但实际没改的。
「多动的文件」= 它没声明却偷偷改了的。

### 3.3 命令行退出码

| 码 | 含义 |
|---|---|
| 0 | PASS |
| 1 | FAIL |
| 2 | 用法错误 |
| 3 | INVALID |
| 4 | NOT RUN |

---

## 4. 侧边栏面板怎么看

从上到下四块：

### 4.1 状态药丸

```
[ Start · 运行中 ]    或    [ Stop · 已停止 ]
```

旁边有 `Start` / `Stop` 按钮。

⚠️ **但要注意**：这里的「运行中」指的是**插件自己拉起的那个审计进程**。Cursor 里的 AI 用的是 `~/.cursor/mcp.json` 里那条配置，是**另一个进程**。两个要各自对齐，别只看药丸就以为万事大吉。

### 4.2 「待判断」区

只列出 `FAIL` 和 `INVALID` 的记录——因为只有这两种需要你表态。

- 默认最多显示 **5 条**
- 多的会折叠成「还有 N 条，点击展开」
- 每条右边三个按钮：

| 按钮 | 什么时候点 |
|---|---|
| **重做** | 我认可这是问题，让 AI 重来 |
| **跳过** | 这条不重要，别再提醒我 |
| **不确定** | 我还没判断，先记下来 |

点完这条就从「待判断」消失，移到下面的「已处理」，同时追加一行到 `notes/l0_events.md`。

⚠️ **「跳过」只是把待办藏起来，不改变事实。** 别拿它当「消红点」。

### 4.3 「已处理」列表

你表态过的记录，带按钮名称和时间。

### 4.4 审计面板（历史）

最近若干条记录，每条显示：

```
2026-11-12T17:34:08+08:00  write_file  【PASS】
意图: modify file verify_0107.txt
```

失败时额外显示原因 / 没动的文件 / 多动的文件。

底部一行统计：

```
最近 20 条：PASS 15  FAIL 3  INVALID 2  NOT_RUN 0
```

---

## 5. 排错

### 5.1 `audit_trace.jsonl` 是空的 / 根本没出现

**最可能的原因：MCP 条目没绑到当前文件夹。**

打开 `~/.cursor/mcp.json` 检查：

```json
{
  "mcpServers": {
    "harness-audit": {
      "command": "/usr/bin/python3",
      "args": [
        "-m", "audit_sidecar.mcp_server",
        "--cwd",   "/你的/项目/绝对路径",
        "--trace", "/你的/项目/绝对路径/audit_trace.jsonl"
      ],
      "cwd": "/你的/项目/绝对路径"
    }
  }
}
```

**关键**：`--cwd` 和 `--trace` 必须存在，且值是你的项目路径。

修复：在那个项目文件夹里重新点一次「一键开启因果审计」，然后重启窗口。

### 5.2 记录跑到了别的文件夹（比如家目录）

**原因**：Cursor 的用户级 MCP 进程**不会**自动把当前打开的文件夹当工作目录，它继承的是编辑器进程的目录（通常是 `$HOME`）。所以必须靠 `--cwd` / `--trace` 显式绑定。

**0.10.5 及更早版本没有写这两个参数**，于是痕迹全落到 `~/audit_trace.jsonl`，而你的项目里那个文件是空的。**0.10.6 起已修复**。

修复：升到 0.11.7，重新点一次一键开启。

### 5.3 面板「运行中」，但 AI 的调用没被审计

参见 4.1：两个进程。检查：

1. `~/.cursor/mcp.json` 里有没有 `harness-audit` 条目
2. 改完之后有没有 **Reload Window**
3. `audit_trace.jsonl` 有没有增长（这是唯一可信的判据）

### 5.4 改了 `mcp.json` 却不生效

**必须重启窗口。** Cursor 只在启动时读 MCP 配置，改文件不会热加载。

### 5.5 一直是 `INVALID`

先看 `reason` 字段，再对症：

| 提示 | 怎么办 |
|---|---|
| 不是 git 仓库 | `git init` 后用 git 见证会升级到 `full` |
| 没有基线 | `harness-audit baseline` |
| 审计器饱和 / 超时 | 减少并发，或调大 `--timeout` |

### 5.6 想确认某次改动是否真落地

看 `audit_trace.jsonl` 最后一行，四个字段：

- `verdict` —— 四值之一
- `changed_files` —— 实际改动的文件
- `missing_expected` —— 声称要改但没改的
- `witness_scope` —— `full`（git 全量）或 `stat`（只有文件存在性/时间，更弱）

---

## 6. 卸载与回滚

### 6.1 只关掉，不卸载

面板上点 **Stop**，即停止插件自己拉起的审计进程。

### 6.2 彻底卸载（四步）

```bash
# 1. 先备份，改坏了能还原
cp ~/.cursor/mcp.json ~/.cursor/mcp.json.bak

# 2. 删掉 harness-audit 这一条（有其他 server 时不要整个文件删掉）
#    手动编辑 ~/.cursor/mcp.json，只删 mcpServers.harness-audit

# 3. 卸载扩展
cursor --uninstall-extension liao-zhizhi.harness-audit

# 4. 重启窗口
```

### 6.3 可选：清理审计产物

这些是审计器生成的文件，删掉不影响你的代码：

```bash
rm -f <项目>/audit_trace.jsonl      # 审计记录
rm -f <项目>/.immune_state.json     # 连续失败计数
rm -f <项目>/notes/l0_events.md     # 待判断区的按钮记录
```

⚠️ 删除前确认 `notes/` 是你自己的目录还是审计器建的。**如果 `notes/` 里还有别的东西，只删 `l0_events.md` 这一个文件。**

---

## 7. 三句话记住

1. **`FAIL` 才是它抓到的假完成。**
2. **`PASS` 只说世界符合意图，不说 AI 干了活。**
3. **`INVALID` 是它拒绝判，不是它坏了。**

---

## 附：速查命令

```bash
# 看最近一条审计结论
python3 -c "import json;print(json.loads(open('audit_trace.jsonl').read().splitlines()[-1])['verdict'])"

# 看当前 MCP 绑定到哪个目录
grep -A6 harness-audit ~/.cursor/mcp.json

# 手动跑一次检查（需先 pipx install harness-audit-sidecar）
harness-audit check --file src/main.py
harness-audit check --command 'pytest -q'
harness-audit check --diff
```
