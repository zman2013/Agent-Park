## 错误经验

### 1. 软链目录浏览被错误拦截
- **错误**: 文件浏览器中不显示软链类型目录；打开软链目录时后端返回 "path outside cwd" 错误，无法浏览软链指向的子目录。根因是路径解析逻辑用 `os.path.realpath` 解析后将结果与 cwd 做严格前缀匹配，软链指向 cwd 外的目标时被 jail 拦截
- **正确做法**: `_resolve_agent_path` 中区分 symlink escape 与真实越狱，允许通过软链访问其子目录（返回 `is_symlink_escape` 标志供调用方决策），而非直接拒绝；前端文件树中正确渲染软链类型节点
- **出现次数**: 2
- **最近发生**: 2026-04-10

### 2. Edit 工具替换字符串不精确
- **错误**: 调用 Edit 工具时 `old_string` 与文件实际内容不匹配（not found）、匹配多处无法确定目标（Found 2 matches）、或替换文本本身含 typo，导致编辑失败或需要多次重试
- **正确做法**: 调用 Edit 前必须先用 Read 确认目标行的精确内容（包括缩进、空行）；`old_string` 应包含足够的周围上下文以保证唯一匹配；替换文本写完后复查是否有拼写错误
- **出现次数**: 7
- **最近发生**: 2026-04-11

### 3. streaming 算子生命周期重叠被忽略
- **错误**: 分析算子间 buffer 生命周期时，默认 A 完全执行完后才开始 B，忽略了 A → B 之间存在 streaming 时两者输入/输出的生命周期是重叠的；MemoryPlan 的 `collectUMUses` 用简单 post-order walk 序号作为 opOrder，未区分 streaming 与非 streaming 边的语义差异，导致 GSDMA descriptor 被 output buffer 覆盖
- **正确做法**: 分析算子依赖时必须先确认是否存在 streaming 连接：存在 streaming 时，A 的输入/输出与 B 的输入/输出生命周期重叠，buffer 地址不可复用；只有不存在 streaming 时才可认为 A 完全执行完后 B 才开始；MemoryPlan 需识别 streaming 边并扩展 buffer lifetime
- **出现次数**: 2
- **最近发生**: 2026-04-11

### 4. 未确认 root cause 就跳转实现
- **错误**: 用户要求先分析 root cause 并 review，agent 在根因尚未明确、用户多次纠正（"不对，等一下"、"先分析出 root cause，然后发我 review"）的情况下仍急于修改代码
- **正确做法**: 收到 "先分析 root cause" 类指令时，先完整定位根因并输出分析报告，等待用户确认后再进入实现阶段；用户说 "等一下" 时应暂停当前假设，倾听补充信息
- **出现次数**: 2
- **最近发生**: 2026-04-11

### 5. 工具调用参数类型错误
- **错误**: 调用 Read 工具时将 `offset` 参数以 string 类型传递，导致 `InputValidationError: expected number but provided as string`
- **正确做法**: 工具调用前确保参数类型严格匹配 schema 定义，数值型参数（offset、limit、pages 等）必须为 int/number 类型，不可用字符串包装
- **出现次数**: 2
- **最近发生**: 2026-04-11

### 6. memory 条目写入方式错误
- **错误**: 将所有 summary 内容合并为一条 memory 条目写入
- **正确做法**: 每个知识（errors、project、hotfiles）独立生成一条 summary，连同索引路径分别写入 memory，使 agent 可以精确引用特定知识
- **出现次数**: 1
- **最近发生**: 2026-04-04

### 8. Gerrit push 缺少 Change-Id
- **错误**: `git push origin HEAD:refs/for/...` 到 Gerrit 时被拒绝，报错 "missing Change-Id in message footer"。根因是 commit message footer 未包含 Change-Id，Gerrit 要求每个提交必须有 Change-Id 才能追踪
- **正确做法**: 首次使用前安装 Gerrit commit-msg hook：`gitdir=$(git rev-parse --git-dir); scp -p -P 29418 user@gerrit.server:hooks/commit-msg ${gitdir}/hooks/`（OpenSSH >= 9.0 需加 `-O` 参数）；安装后执行 `git commit --amend --no-edit` 自动补入 Change-Id，再重新 push
- **出现次数**: 1
- **最近发生**: 2026-04-11

### 9. 文件搜索不支持绝对路径
- **错误**: 文件搜索窗口仅显示 cwd 下的相对路径，不支持绝对路径，限制了用户在非 cwd 目录下搜索文件的能力
- **正确做法**: 文件搜索和路径展示应支持绝对路径，不再限定为 cwd 的子目录或文件；前端 `fullCurrentPath` 计算时需判断 `currentDirPath` 是否为绝对路径，若是则直接使用
- **出现次数**: 1
- **最近发生**: 2026-04-10

### 10. knowledge_summary 自我引用产生噪音
- **错误**: knowledge_summary 生成的内容被当作知识信号再次提取，形成自引用循环，噪音污染知识库
- **正确做法**: 在 `extract_error_signals` 和 `extract_project_signals` 中增加 `_AGENT_PARK_NOISE_KEYWORDS` 过滤列表，拦截包含 "knowledge_summary"、"知识总结"、"build_memory_entries" 等内部实现关键词的消息片段
- **出现次数**: 1
- **最近发生**: 2026-04-09
