# Android Use MCP — TODO

## 目标

从 `minitap-ai/mobile-use` 中提取 Android 设备控制能力，构建一个本地 `stdio` MCP Server，使 Codex、Claude Code 等宿主 Agent 能直接观察并操作 Android 真机或模拟器。

宿主 Agent 负责视觉理解、任务规划、操作决策和失败恢复；MCP Server 只提供确定性的设备观察与操作能力，不调用任何 LLM。

## 固定范围

- [x] Android only
- [x] 只提供 MCP，不提供 CLI
- [x] 本地 `stdio` transport
- [x] 支持 Android 真机和模拟器
- [x] 复用原项目的 ADB、uiautomator2、Controller、selector fallback 和文本输入逻辑
- [x] 不内置或配置 OpenAI、Anthropic、Google 等模型
- [x] 不引入 LangChain、LangGraph、Minitap Cloud、Limrun、BrowserStack 或 PostHog
- [x] 不包含 iOS、IDB、WDA
- [x] 默认不暴露任意 `adb shell`

## 目标目录结构

```text
android-use-mcp/
├── pyproject.toml
├── README.md
├── LICENSE
├── NOTICE
├── src/android_use_mcp/
│   ├── __init__.py
│   ├── __main__.py
│   ├── server.py
│   ├── session.py
│   ├── models.py
│   ├── errors.py
│   ├── android_client.py
│   ├── controller.py
│   ├── snapshot.py
│   ├── selectors.py
│   └── tools/
│       ├── device.py
│       ├── observe.py
│       └── actions.py
└── tests/
    ├── unit/
    └── integration/
```

## Phase 0：项目骨架与许可

预计：30–45 分钟

- [ ] 在研究目录下创建独立 `android-use-mcp/` 项目
- [ ] 使用 Python 3.12+ 和官方 Python MCP SDK/FastMCP
- [ ] 配置最小依赖：`mcp`、`adbutils`、`uiautomator2`、`pydantic`、`pillow`
- [ ] 配置 Ruff、Pyright 和 Pytest
- [ ] 从上游复制 Apache 2.0 `LICENSE`、`NOTICE`，保留来源和修改说明
- [ ] 确保 MCP 日志只写 stderr，不污染 stdio JSON-RPC
- [ ] 添加 `.gitignore`，排除 `.venv`、截图、trace 和临时文件

### 验收

- [ ] MCP Server 能启动并完成 initialize/list_tools
- [ ] 未导入 LangChain、LangGraph 或任何模型 SDK

## Phase 1：Android Core 提取

预计：1.5–2 小时

### 设备与连接

- [ ] 提取并精简 `UIAutomatorClient`
- [ ] 提取并精简 `AndroidDeviceController`
- [ ] 新建 `AndroidSession`，持有 device ID、ADB client、uiautomator2 client 和屏幕信息
- [ ] 支持列出 `device`、`offline`、`unauthorized` 状态
- [ ] 未指定设备时，仅在唯一在线设备存在时自动连接
- [ ] 多设备时返回候选列表，不静默选择第一台
- [ ] 实现 session cleanup

### UI 数据

- [ ] 复用 screenshot + hierarchy 联合读取
- [ ] 统一 UI element 字段：text、content description、resource ID、class、bounds、clickable、enabled、focused、selected
- [ ] 将 bounds 标准化为 `{x, y, width, height}`
- [ ] 过滤不可见、无内容且无交互意义的节点
- [ ] 设置最大节点数和最大文本长度，防止 MCP 输出失控

### Selector

- [ ] 复用 `Target`：bounds、resource ID、text、index
- [ ] 复用坐标范围校验
- [ ] 复用 resource ID 递归查找
- [ ] 复用 text 大小写不敏感查找
- [ ] 保留 selector fallback 的每次尝试及错误信息

### 文本输入

- [ ] 复用输入框 focus 检查
- [ ] 点击后重新读取 hierarchy 验证 focused 状态
- [ ] 优先使用 uiautomator2 输入
- [ ] 保留安全转义后的 ADB 输入 fallback
- [ ] 支持清空全部文本和删除指定字符数

### 验收

- [ ] Core 不依赖 MCP 也能被单元测试直接调用
- [ ] 所有外部参数使用 Pydantic 校验
- [ ] 不存在 `shell=True` 和任意字符串命令执行接口

## Phase 2：MCP 工具集

预计：1.5–2 小时

### 设备工具

- [ ] `android_list_devices`
- [ ] `android_connect`
- [ ] `android_status`
- [ ] `android_disconnect`

### 观察工具

- [ ] `android_snapshot`
  - [ ] 返回 MCP image content 截图
  - [ ] 返回结构化 UI elements
  - [ ] 返回屏幕尺寸
  - [ ] 返回前台 package/activity
  - [ ] 支持 `interactive_only`
  - [ ] 支持 `max_elements`
- [ ] `android_screenshot`
- [ ] `android_get_ui_elements`
- [ ] `android_get_foreground_app`
- [ ] `android_list_apps`
  - [ ] 支持 query 过滤
  - [ ] 不使用内部 LLM 推断 App package

### 操作工具

- [ ] `android_tap`
- [ ] `android_long_press`
- [ ] `android_swipe`
- [ ] `android_type_text`
- [ ] `android_clear_text`
- [ ] `android_press_key`
- [ ] `android_launch_app`
- [ ] `android_terminate_app`
- [ ] `android_open_url`
- [ ] `android_wait`

### 工具语义

- [ ] 所有工具使用 `android_` 前缀
- [ ] 所有工具返回结构化 `success`、`error_code`、`message` 和必要数据
- [ ] 操作失败时建议重新调用 `android_snapshot`
- [ ] 标记只读、写入、幂等和破坏性 hints
- [ ] 单个 session 内写操作串行化，防止同时点击/输入
- [ ] 为每个调用配置合理 timeout

### 验收

- [ ] MCP Inspector 能列出并调用所有工具
- [ ] 截图被客户端识别为图像，而不是一段 base64 文本
- [ ] UI hierarchy 不包含完整截图 base64
- [ ] 错误不会泄露内部堆栈、环境变量或敏感输入

## Phase 3：稳定性与安全

预计：1.5–2.5 小时

- [ ] 检测设备中途断开、offline 和 unauthorized
- [ ] 操作前检查 session 是否仍有效
- [ ] 为 uiautomator2 临时失败提供一次受控重连
- [ ] 启动 App 后轮询前台 package，保留原项目 retry 逻辑
- [ ] 输入、点击、滑动参数增加长度和范围限制
- [ ] URL 只接受合法 scheme
- [ ] 日志不记录密码、验证码、完整输入文本或截图内容
- [ ] 默认不提供以下工具：
  - [ ] 任意 ADB shell
  - [ ] APK 安装
  - [ ] App 卸载
  - [ ] 清除 App 数据
  - [ ] 重启/关机
  - [ ] 恢复出厂设置
- [ ] MCP 退出、异常和取消时正确清理 session

### 验收

- [ ] 拔掉设备后工具返回可行动的 `DEVICE_DISCONNECTED`
- [ ] 多设备连接时不会误操作未指定设备
- [ ] 非法坐标、超长文本和非法 URL 被 Schema 层拒绝

## Phase 4：测试

预计：2–3 小时

### 单元测试

- [ ] bounds 解析和中心点计算
- [ ] resource ID 查找及重复 index
- [ ] text 查找及重复 index
- [ ] selector fallback 顺序和错误聚合
- [ ] 坐标范围校验
- [ ] UI element 过滤和输出限额
- [ ] ADB 文本输入转义
- [ ] device/session 状态转换
- [ ] MCP 输入 Schema
- [ ] 结构化错误输出

### Mock 集成测试

- [ ] list/connect/disconnect 生命周期
- [ ] snapshot 同时返回 image 和 structured content
- [ ] tap、swipe、type、press key 调用正确 Controller 方法
- [ ] disconnected/offline/unauthorized 错误路径
- [ ] MCP Server shutdown cleanup

### 真机或模拟器测试

- [ ] 连接一台启用 USB debugging 的设备
- [ ] 获取截图和 UI hierarchy
- [ ] 打开 Android Settings
- [ ] 通过 resource ID 或 text 点击设置项
- [ ] 滑动页面
- [ ] 聚焦输入框并输入中文、英文、空格和特殊字符
- [ ] back/home/enter
- [ ] 启动、停止 App 和打开 URL
- [ ] 断开设备后验证错误恢复

## Phase 5：Codex 与 Claude Code 接入

预计：1–1.5 小时

- [ ] 提供 Codex `config.toml` stdio MCP 配置示例
- [ ] 提供 Claude Code MCP 配置示例
- [ ] Codex 能发现全部工具
- [ ] Claude Code 能发现全部工具
- [ ] 验证宿主 Agent 能读取 snapshot 图像和 UI elements
- [ ] 用 Codex 完成一个多步骤 Android 任务
- [ ] 用 Claude Code 完成同等任务
- [ ] 记录两者对截图、UI tree 和错误返回的兼容差异

### 建议验收任务

```text
打开 Android 设置，进入关于手机页面，找到并返回设备型号。
```

该任务至少需要：启动 App、读取 snapshot、点击、滚动、重新观察和提取文本。

## Phase 6：文档与交付

预计：1–1.5 小时

- [ ] README：项目目标和非目标
- [ ] README：Android/ADB/uiautomator2 前置条件
- [ ] README：Codex 配置
- [ ] README：Claude Code 配置
- [ ] README：工具清单和调用示例
- [ ] README：真机授权、unauthorized 和 offline 排障
- [ ] README：截图和敏感数据说明
- [ ] README：已知限制
- [ ] 生成源码复用与修改说明
- [ ] 最终运行 Ruff、Pyright、Pytest 和 MCP Inspector
- [ ] 确认 Git 工作区不包含截图、设备数据、API key 或临时日志

## 不进入首版的增强项

- [ ] snapshot 临时元素引用，如 `e12`
- [ ] stale element 检测
- [ ] `android_wait_for_text`
- [ ] `android_wait_for_element`
- [ ] `android_wait_for_ui_change`
- [ ] UI diff
- [ ] 多设备并行 session
- [ ] 视频录制
- [ ] Logcat
- [ ] APK 安装与管理
- [ ] 可选受限 ADB shell
- [ ] HTTP MCP transport
- [ ] 发布 PyPI 包

## 预计时间

| 交付级别 | 预计时间 |
|---|---:|
| 可演示 MVP | 4–6 小时 |
| 包含测试、文档和接入验证的完整版本 | 1–2 个工作日 |
| 多设备、多 ROM、不同输入法兼容性打磨 | 持续按实际设备补充 |

## Definition of Done

- [ ] 项目不要求任何模型 API Key
- [ ] 项目没有 LangChain、LangGraph、云设备和遥测依赖
- [ ] Codex 和 Claude Code 均可通过 stdio MCP 启动 Server
- [ ] Agent 能获得当前屏幕截图和精简 UI hierarchy
- [ ] Agent 能完成点击、滑动、输入、按键和 App 生命周期操作
- [ ] 失败结果包含明确错误码和下一步建议
- [ ] 一台真实 Android 设备或模拟器完成端到端验收任务
- [ ] 所有非设备集成测试通过
- [ ] README、LICENSE、NOTICE 和来源说明齐全
