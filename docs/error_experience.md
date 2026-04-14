## 错误经验

### 1. Edit 替换字符串不精确 (8次)
Edit 的 `old_string` 与文件内容不匹配/多处匹配/含 typo
→ Edit 前用 Read 确认精确内容；`old_string` 含足够上下文保证唯一匹配

### 2. Write 前未 Read (5次)
对已有文件调用 Write 未先 Read
→ Write/Edit 前必须先 Read

### 3. 假设路径存在未验证 (5次)
引用不存在的路径（wiki 页面/MLIR 产物等），触发 `Path does not exist` / `SOURCE_NOT_FOUND`
→ 使用前用 Glob/Grep 验证路径

### 4. Gerrit push 遗漏 (4次)
push 被拒（non-fast-forward / missing Change-Id / 缺 Feishu-Url）
→ push 前 fetch；落后时先 pull --rebase；commit 含 Change-Id 和 Feishu-Url

### 5. 用户说查 wiki 未执行 (4次)
跳过 wiki 直接分析；或 query 过于宽泛得无关结果
→ 先 search_memory / Glob / Read；query 聚焦具体问题

### 6. 工具参数错误 (3次)
参数类型错误或参数名不存在，触发 InputValidationError
→ 参数名称和类型严格匹配 schema

### 7. 误解日志目标 (3次)
"分析编译耗时"指向运行时日志而非 build.sh 输出
→ "编译耗时"指 build.sh 输出；"stop, 我是指 X"立即切换

### 8. e2e-test case 名未验证 (3次)
使用非标准 case 名触发 `Case not found`
→ 先确认 case 在 cases.txt 中，或直接用 .mlir 完整路径

### 9. streaming 生命周期重叠 (2次)
忽略 streaming 连接下 A→B 生命周期重叠，buffer 被覆盖
→ 先确认 streaming 边；存在时 buffer 不可复用

### 10. 大文件未分段读取 (2次)
>256KB 文件直接 Read 触发 size 超限
→ 先用 wc 预估大小，再用 offset/limit 分段读，或 Grep 搜索
