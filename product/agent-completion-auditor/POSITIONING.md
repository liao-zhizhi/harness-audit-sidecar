# Agent Completion Auditor

Catches false completion in coding agents. It audits the claim, not the trace.

## What it does

When an agent says it finished, this product audits that claim with independent physical witnesses: file hashes, mtime, test exit codes, git diff. It emits `PASS` / `FAIL` / `INVALID` / `NOT_RUN`, and lights a red or green lamp in the editor.

It does not score the session. It checks whether the claimed completion matches what the filesystem and process actually did.

## What it does not do

These boundaries keep it from being read as a general platform:

- Not an observability product. No traces, eval scores, or dashboards.
- Not an MCP security scanner. It does not hunt tool poisoning or config holes.
- Not a code-review or test-generation tool. It does not judge code quality. It only judges whether “I completed it” matches the physical effect.

## Difference from existing tools

| Kind of tool | They answer | We answer |
| --- | --- | --- |
| Observability platforms | What did the agent call, how long, at what score? | Did the completion claim match a physical witness? |
| MCP security scanners | Is this tool or config poisoned or unsafe? | Did the write or command actually land as claimed? |
| Code verification tools | Is this code good, tested, or spec-correct? | Did “done” match file hash, mtime, exit code, or git diff? |

## Who it is for

Developers who write daily in Cursor, Claude Code, or Windsurf, and keep hitting “the AI said it fixed it, but the file did not change / the test did not run.”

## One measured fact

In a live test, after external rules were removed, the model wrote directly and failed in 2 of 5 runs, and did not request a physical witness. With pain injection, avoidance rose from 0/5 to 5/5. That is a measured gap, not a claim that the problem is gone.
