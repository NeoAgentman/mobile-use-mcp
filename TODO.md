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
mobile-use-mcp/
├── pyproject.toml
├── README.md
├── LICENSE
├── NOTICE
├── src/mobile_use_mcp/
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

- [x] 在研究目录下创建独立 `mobile-use-mcp/` 项目
- [x] 使用 Python 3.12+ 和官方 Python MCP SDK/FastMCP
- [x] 配置最小依赖：`mcp`、`adbutils`、`uiautomator2`、`pydantic`、`pillow`
- [x] 配置 Ruff、Pyright 和 Pytest
- [x] 从上游复制 Apache 2.0 `LICENSE`、`NOTICE`，保留来源和修改说明
- [x] 确保 MCP 日志只写 stderr，不污染 stdio JSON-RPC
- [x] 添加 `.gitignore`，排除 `.venv`、截图、trace 和临时文件

### 验收

- [x] MCP Server 能启动并完成 initialize/list_tools
- [x] 未导入 LangChain、LangGraph 或任何模型 SDK

## Phase 1：Android Core 提取

预计：1.5–2 小时

### 设备与连接

- [x] 提取并精简 `UIAutomatorClient`
- [x] 提取并精简 `AndroidDeviceController`
- [x] 新建 `AndroidSession`，持有 device ID、ADB client、uiautomator2 client 和屏幕信息
- [x] 支持列出 `device`、`offline`、`unauthorized` 状态
- [x] 未指定设备时，仅在唯一在线设备存在时自动连接
- [x] 多设备时返回候选列表，不静默选择第一台
- [x] 实现 session cleanup

### UI 数据

- [x] 复用 screenshot + hierarchy 联合读取
- [x] 统一 UI element 字段：text、content description、resource ID、class、bounds、clickable、enabled、focused、selected
- [x] 将 bounds 标准化为 `{x, y, width, height}`
- [x] 过滤不可见、无内容且无交互意义的节点
- [x] 设置最大节点数和最大文本长度，防止 MCP 输出失控

### Selector

- [x] 复用 `Target`：bounds、resource ID、text、index
- [x] 复用坐标范围校验
- [x] 复用 resource ID 查找
- [x] 复用 text 大小写不敏感查找
- [x] 保留 selector fallback 的每次尝试及错误信息

### 文本输入

- [x] 复用输入框 focus 检查
- [x] 点击后重新读取 hierarchy 验证 focused 状态
- [x] 优先使用 uiautomator2 输入
- [x] 保留参数化 ADB 输入 fallback
- [x] 支持删除指定字符数

### 验收

- [x] Core 不依赖 MCP 也能被单元测试直接调用
- [x] 所有外部参数使用 Pydantic/FastMCP Schema 校验
- [x] 不存在 `shell=True` 和任意字符串命令执行接口

## Phase 2：MCP 工具集

预计：1.5–2 小时

### 设备工具

- [x] `android_list_devices`
- [x] `android_connect`
- [x] `android_status`
- [x] `android_disconnect`

### 观察工具

- [x] `android_snapshot`
  - [x] 返回 MCP image content 截图
  - [x] 返回结构化 UI elements
  - [x] 返回屏幕尺寸
  - [x] 返回前台 package/activity
  - [x] 支持 `interactive_only`
  - [x] 支持 `max_elements`
- [x] `android_screenshot`
- [x] `android_get_ui_elements`
- [x] `android_get_foreground_app`
- [x] `android_list_apps`
  - [x] 支持 query 过滤
  - [x] 不使用内部 LLM 推断 App package

### 操作工具

- [x] `android_tap`
- [x] `android_long_press`
- [x] `android_swipe`
- [x] `android_type_text`
- [x] `android_clear_text`
- [x] `android_press_key`
- [x] `android_launch_app`
- [x] `android_terminate_app`
- [x] `android_open_url`
- [x] `android_wait`

### 工具语义

- [x] 所有工具使用 `android_` 前缀
- [x] 所有工具返回结构化 `success`、`error_code`、`message` 和必要数据
- [x] 操作失败时建议重新调用 `android_snapshot`
- [x] 标记只读、写入、幂等和破坏性 hints
- [x] 单个 session 内写操作串行化，防止同时点击/输入
- [x] 为设备发现、启动轮询和 MCP 端测配置 timeout

### 验收

- [x] MCP Inspector 能列出所有工具，stdio 端测能调用工具
- [x] 截图被 MCP 客户端识别为图像，而不是一段 base64 文本
- [x] UI hierarchy 不包含完整截图 base64
- [x] 错误不会泄露内部堆栈、环境变量或敏感输入

## Phase 3：稳定性与安全

预计：1.5–2.5 小时

- [x] 检测设备中途断开、offline 和 unauthorized
- [x] 操作前检查 session 是否仍有效
- [x] 为 uiautomator2 临时失败提供一次受控重连
- [x] 启动 App 后轮询前台 package，保留原项目 retry 逻辑
- [x] 输入、点击、滑动参数增加长度和范围限制
- [x] URL 只接受合法 scheme
- [x] 日志不记录密码、验证码、完整输入文本或截图内容
- [x] 默认不提供以下工具：
  - [x] 任意 ADB shell
  - [x] APK 安装
  - [x] App 卸载
  - [x] 清除 App 数据
  - [x] 重启/关机
  - [x] 恢复出厂设置
- [x] 显式 disconnect 时正确清理 session；stdio 进程退出不保留外部子进程

### 验收

- [x] 拔掉设备后工具返回可行动的 `DEVICE_DISCONNECTED`
- [x] 多设备连接时不会误操作未指定设备
- [x] 非法坐标、超长文本和非法 URL 被 Schema/Controller 层拒绝

## Phase 4：测试

预计：2–3 小时

### 单元测试

- [x] bounds 解析和中心点计算
- [x] resource ID 查找及重复 index
- [x] text 查找及重复 index
- [x] selector fallback 顺序和错误聚合
- [x] 坐标范围校验
- [x] UI element 过滤和输出限额
- [x] 参数化 ADB 文本输入 fallback
- [x] device/session 状态转换
- [x] MCP 输入 Schema
- [x] 结构化错误输出

### Mock 集成测试

- [x] list/connect/disconnect 生命周期
- [x] snapshot 同时返回 image 和 structured content
- [x] tap、swipe、type、press key 调用正确 Controller 方法
- [x] disconnected/offline/unauthorized 错误路径
- [x] stdio MCP 子进程 initialize/list_tools/call_tool 生命周期

### 真机或模拟器测试

- [x] 连接一台启用 USB debugging 的设备
- [x] 获取截图和 UI hierarchy
- [x] 打开 Android Settings
- [x] 通过 resource ID 或 text 点击设置项
- [x] 滑动页面
- [x] 聚焦输入框并输入中文、英文、空格和特殊字符
- [x] back/home/enter
- [x] 启动 App
- [x] 停止 App 和打开 URL
- [x] 断开设备后验证错误恢复

## Phase 5：Codex 与 Claude Code 接入

预计：1–1.5 小时

- [x] 提供 Codex `config.toml` stdio MCP 配置示例
- [x] 提供 Claude Code MCP 配置示例
- [x] Codex 能发现全部工具
- [x] Claude Code MCP health check 能发现并连接 Server
- [x] 验证宿主 Agent 能读取 snapshot 图像和 UI elements
- [x] 用 Codex 完成一个多步骤 Android 任务
- [ ] 用 Claude Code 完成同等任务
- [x] 在 `COMPATIBILITY.md` 记录当前兼容性和 Claude Code 登录阻塞

### 建议验收任务

```text
打开 Android 设置，进入关于手机页面，找到并返回设备型号。
```

该任务至少需要：启动 App、读取 snapshot、点击、滚动、重新观察和提取文本。

## Phase 6：文档与交付

预计：1–1.5 小时

- [x] README：项目目标和非目标
- [x] README：Android/ADB/uiautomator2 前置条件
- [x] README：Codex 配置
- [x] README：Claude Code 配置
- [x] README：工具清单和调用示例
- [x] README：真机授权、unauthorized 和 offline 排障
- [x] README：截图和敏感数据说明
- [x] README：已知限制
- [x] 生成源码复用与修改说明
- [x] 最终运行 Ruff、Pyright、Pytest 和 MCP Inspector
- [x] 确认 Git 工作区不包含截图、设备数据、API key 或临时日志

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

- [x] 项目不要求任何模型 API Key
- [x] 项目没有 LangChain、LangGraph、云设备和遥测依赖
- [x] 通用 MCP 客户端可通过 stdio MCP 启动 Server；Codex/Claude 配置已提供
- [x] Server 能以 MCP image content 和结构化结果返回截图/UI hierarchy
- [x] 点击、滑动、输入、按键和 App 生命周期操作已实现并通过 Mock 测试
- [x] 失败结果包含明确错误码和下一步建议
- [x] 一台真实 Android 设备完成端到端验收任务
- [x] 所有非设备集成测试通过
- [x] README、LICENSE、NOTICE 和来源说明齐全
