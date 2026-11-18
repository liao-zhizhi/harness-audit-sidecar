# 检查回执

Cursor 和其它编码智能体在声明「完成」之前，必须先用已经接入 harness 审计的 `run_command`（或等价入口）跑项目的测试或检查。轨迹里的 `check_receipt` 只记录这一次调用像不像检查，供下游阅读。`present: false` 不会改四值判词，也不会拦下 PASS。

写盘工具（`write_file`、`append_file`，以及会改文件的 `run_command`）仍要声明 `expected_files_modified`。文件内容里出现 `pytest` 不算跑过检查。

字段形状：

```json
"check_receipt": {"present": false, "kind": null, "detail": ""}
```

`present` 为 true 时，`kind` 取一：`pytest`、`unittest`、`npm_test`、`cargo_test`、`go_test`、`ctest`、`generic_test`。`tox` 和 `nox` 记为 `generic_test`。

## 可粘贴进 `.cursor/rules` 的短规则

```
未留下测试/检查回执不得声称完成；声称完成前先跑 pytest 或等价命令（unittest、npm test、cargo test、go test、ctest、tox、nox），并且必须经由已接入 harness 审计的 run_command。写盘时仍要声明 expected_files_modified。
```
