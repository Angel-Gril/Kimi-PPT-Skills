## [Unreleased]

### 新增
- 宿主侧导出通道（可选）：`export_host.html?hostExport=1` 会向编辑器声明 `sdkExportMode=external`
  并实现 `handleExport`，使 SDK 模式在**无 Kimi 账号**时也能导出 PPTX（编辑器降级为渲染器）。
- `scripts/export_pptx_host.py`：无登录、无专用 Profile 的导出入口，支持 `--pptx`（外部产物）
  与 `--auto-generate`（内置参考渲染器）。
- `scripts/pptd_to_pptx.py`：PPTX 参考渲染器（文本 / 背景 / 淡入淡出 / 页序），仅作占位实现。

### 说明
- 默认行为未变：不带 `?hostExport=1` 时与旧路径完全一致，仍需登录走官方服务端渲染。
- 参考渲染器**不产出**官方级画质（无图片/形状/图表/元素动画，不嵌字体）；需要官方效果请继续使用 `export_pptx.py`。

# 更新日志

[简体中文](CHANGELOG.md) | [English](CHANGELOG_EN.md)

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.2.0] - 2026-08-12

### 新增

- 基于 Playwright 的稳定 PPTX / 页面图片导出，不再依赖 `agent-browser` CLI 会话
- 专用 Chromium Profile 首次登录与后续复用，解决本地宿主中 Kimi iframe 的第三方存储分区认证问题
- 导出后统计独立文本、形状、图片、表格和图表对象，默认拒绝整页图片型 PPTX
- 针对签名接口 401、登录过期、SDK 状态错误、下载超时的明确诊断

### 安全

- 登录态与分区状态保存在专用 Profile 中以便复用；Token 仅在运行时读取并注入 iframe 分区，不打印、不写入 PPTD 或日志、不提交到仓库
- 自动化不连接用户日常浏览器，失败 URL 会移除查询参数和片段
- 新增发布前敏感信息扫描与严格忽略规则
- 新增专用浏览器 Profile 签名所有权标记、崩溃安全的导出/安装锁、OOXML 关系图验收、ZIP 资源上限与事务式输出提交
- 页面图片 QA 输出现在必须完全位于 PPTD 项目树之外；默认使用同级目录 `<project-name>.qa-images`

### 修复

- 修复 Kimi 编辑器在 `127.0.0.1` 顶层页中因 Chrome 分区存储无法读取登录态，导致 `/apiv2/utils/v1/signatures` 返回 401 的问题
- 修复 PPT Blob 下载无法被普通点击或 `agent-browser download` 稳定捕获的问题
- 修复编辑器持续网络活动导致 CLI 串行等待和随机超时的问题

## [1.1.1] - 2026-08-06

### 新增

- PPTD 对齐官方元素级 `animations` 规范；Skill 补充动画 / `notes` 使用边界
- 对齐配图优先级、文风禁令、澄清提问、复刻细则、并行写页等官方策略
- 内置约 30 套 preset design system，并支持点名使用
- 恢复 `customFonts`（Google Fonts）与海报推荐尺寸
- 根目录主题目录：`theme.md` / `theme_EN.md`（含预览图）
- 示例项目 `example/xiaomi-yu7-ppt-animation`（页内元素入场动画）

### 变更

- 场景文档同步官方动画细则与 `customFonts` 引用
- README 补充元素动画、预设主题说明与示例 Prompt

## [1.0.2] - 2026-08-06

### 变更

- `install` 默认直接覆盖已安装的 Skill，无需再加 `--force`（旧的 `--force` 仍可兼容传入）

## [1.0.1] - 2026-08-06

### 新增

- Skill 工作流增加 **step0 本地前置检测**：生成前检查 Node.js 18+、npm/npx、python3，并提示需要 Chromium 系浏览器
- 导出脚本在启动时检测 **Node.js 18+** 与 **npm**；缺失或版本过低时给出明确安装指引
- CLI（`open-kimi-ppt-skills`）启动时校验 Node.js 主版本 ≥ 18
- 缺失 **PyYAML** 时自动 `pip install --user pyyaml`（与 Pillow / websocket-client 行为一致）

### 文档

- 补充多 Agent / 多模型实测截图（ChatGPT·Codex + 5.6 Luna、Reasonix + DeepSeek、WorkBuddy 等）
- 安装说明改为「自动 / 手动二选一」，并补充 Windows 路径说明
- README 结构与示例图更新

## [1.0.0] - 2026-08-05

### 新增

- 首次发布 `open-kimi-ppt-skills`
- PPTD 生成 / 编辑 / 复刻，默认同时交付可编辑 PPTD 项目与 PPTX 成品
- 浏览器侧导出 PPTX（嵌字体、淡入淡出切换），导出前可选多模态视觉质检
- 本地在线 PPTD 编辑器（`npx open-kimi-ppt-skills serve`）
- CLI 安装 Skill 到 `~/.agents/skills`（可用 `--target` 指定其他 Agent 目录）
