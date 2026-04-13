---
name: schumacher-compiler 微基准测试项目
description: Schumacher 编译器 CI 项目中 single op 微基准测试的目录结构、CSV 配置和提交流程
type: project
---

## 项目目录结构

- `workloads/<model>/<case_name>/<case_name>.mlir` — MLIR 微基准测试用例
- `models_single_op_fm.csv` / `models_single_op_ccu.csv` — single op 测试用例配置，分别用于 fm（融合矩阵）和 ccu（计算单元）两种调度路径
- `tools/deploy/configs/NeoLowering.yaml` — Neo lowering 编译配置

## CSV 配置约定

- 每行格式: `<arch>,<model>,<op>,...,<priority>,<precision_type>,<tile>,<lowering_config>,<timeout>,<scheduler>,...SCHEDULE`
- fm 版本和 ccu 版本结构基本相同，ccu 版本的 `simulate_tile_relocation` 列为空
- 新增测试用例时需同时追加到两个 CSV 文件末尾

## Git 提交流程

- Gerrit review 使用 `git push origin HEAD:refs/for/tmp/neo` 推送到临时分支
- 推送成功后会返回 Gerrit review 链接
