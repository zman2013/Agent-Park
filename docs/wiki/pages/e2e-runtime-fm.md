---
title: "E2E 运行时：FM 与 sch-driver"
summary: "介绍 E2E 测试中 FM 软件仿真器和 sch-driver 统一执行驱动器的概念、架构、配置、执行流程和错误码。"
overview: "该页面是编译器 E2E 测试运行时的参考文档，覆盖两大核心组件：FM（Functional Model，NPU 软件功能仿真器）和 sch-driver（多平台统一执行驱动器）。内容涵盖：FM 与 sch-driver 的架构关系（FM 是 sch-driver 内部的平台模式而非独立进程）、Backend 枚举定义（FM/SOC/REMOTE_SOC/CCU/REMOTE_CCU 五种模式）、sch-driver 双层配置体系（平台级 fm_config.json 与仿真器行为级 fm_cfg.xml）、完整的 E2E 执行流程（编译→数据准备→sch-driver 执行→精度/延迟校验）、sch-llvm 工具链版本管理与依赖下载（v3.1.3，JFrog Artifactory）、sch-driver 错误码说明（常见 -4 精度验证失败），以及切换和使用自定义 FM 的多种方式。适用于理解编译器 E2E 测试中仿真执行的架构设计、修改或调试 FM 仿真行为、以及管理编译器工具链版本。"
tags: ["testing", "toolchain", "runtime", "reference", "concept"]
sources: []
created: "2026-04-12"
updated: "2026-04-13"
---

# E2E 运行时：FM 与 sch-driver

## FM 与 sch-driver 的关系

### 核心概念

- **FM (Functional Model)**：Schumacher NPU 的软件功能仿真器，在 x86 主机上模拟 NPU 硬件行为执行编译产物。
- **sch-driver**：统一执行驱动器，提供多后端抽象，负责加载 ELF 编译产物、加载输入数据、执行推理并比对结果。

### 架构关系

FM **不是独立进程**，而是封装在 sch-driver 内部的一种**执行平台模式**。sch-driver 通过 `--platform-config` 参数区分 FM 仿真和 SOC 硬件执行，同一驱动器支持多种后端。

sch-driver 的完整工作流程：

1. 加载 ELF 编译产物
2. 加载输入数据（data.json 描述的二进制文件）
3. 在指定平台执行推理（FM 仿真 或 SOC 硬件）
4. 比对输出与 golden 数据
5. 报告性能数据

### Backend 枚举

sch-driver 支持五种后端模式：

| Backend | 说明 |
|---------|------|
| `FM` | 本地功能仿真（x86 上模拟 NPU） |
| `SOC` | 本地真实硬件执行 |
| `REMOTE_SOC` | 远程 SOC 设备执行 |
| `CCU` | CCU 仿真模式 |
| `REMOTE_CCU` | 远程 CCU 设备执行 |

### FM 切换方式

通过 `--platform-config` 参数指定不同配置文件实现模式切换：

- **FM 仿真**：`fm_config.json`
- **SOC 硬件**：`evb_config.json`

## sch-driver 双层配置体系

sch-driver 使用双层配置体系，将平台配置与仿真行为解耦。

### 第一层：平台级配置（fm_config.json）

定义执行平台的基本参数，包含 `"platform": "fm"` 等字段，决定使用哪种后端执行模式。包括硬件拓扑、内存布局、Tile 配置等环境参数。

### 第二层：仿真器行为级配置（fm_cfg.xml）

控制 FM 仿真器的具体行为细节，包括：

- 仿真精度（浮点精度、量化参数）
- 性能模拟参数（延迟模型、吞吐限制）
- 调试开关（日志级别、trace 输出）

### 设计优势

- **关注点分离**：平台配置与仿真行为解耦
- **灵活性**：可独立调整仿真行为而不影响平台配置
- **可扩展性**：新增平台模式时只需添加对应配置文件

## 完整的 E2E 执行流程

```
MLIR 源码 → 编译 → ELF + .c.inc + sch-driver
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              加载编译产物     加载输入数据      加载 golden
              (ELF/schedule)  (data.json)      (golden 文件)
                    │               │               │
                    └───────────────┼───────────────┘
                                    ▼
                            sch-driver 执行
                            (--platform-config)
                                    │
                    ┌───────────────┼───────────────┐
                    ▼                               ▼
              FM 仿真模式                    SOC 硬件模式
              (x86 模拟)                      (真实 NPU)
                    │                               │
                    └───────────────┬───────────────┘
                                    ▼
                            比对输出 vs golden
                            报告 pass/fail + 性能
```

## sch-llvm 工具链版本管理

### 依赖管理

- 通过 `DEPS` 文件声明版本号
- 从 JFrog Artifactory 下载预编译包
- **当前版本**：v3.1.3

### 安装结构

- 预编译包包含 sch-driver 和 FM 实现
- 安装路径：`bin/sch-driver`
- 创建方式：`install.sh` 从 `sch-llvm/` 符号链接创建

### 设计考量

使用外部制品库（Artifactory）管理预编译工具链，确保编译环境一致性和版本可追溯性。FM 的实现也封装在该工具链包中。

## sch-driver 错误码

| 错误码 | 含义 | 常见原因 |
|--------|------|----------|
| `-4` | 精度验证失败 | 输出与 golden 数据差异超过阈值 |

## 切换和使用自定义 FM

通过 `--platform-config` 参数指定不同的配置文件即可切换执行模式：

```bash
# 使用 FM 仿真
sch-driver --platform-config fm_config.json ...

# 使用 SOC 硬件
sch-driver --platform-config evb_config.json ...
```
