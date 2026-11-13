# 验证记录

验证日期：2026-11-13。发布版本：0.3.0。

## 代码验证

- Windows 本机管理员环境的完整基线：Python 3.12.4、Git 2.56.0.windows.4，执行 `python -X utf8 scripts/run_tests.py`，**83 项全部通过，0 skipped**，耗时 286.606 秒。入口先创建真实文件和目录符号链接，缺少权限直接失败。
- 英文 Windows 的 cp1252 管道问题已修复为 CLI 统一 UTF-8 输出；新增 6 项回归，与两个入口及原有帮助检查合计 10 项本机复测全部通过。当前完整套件共 89 项，由下述双平台 CI 执行。
- 覆盖首次绑定、三方冲突与备份、双 clone 同步与失败保留、孤立绑定清理、保留命名空间、任务与交接、真实 autocrlf 克隆、缓存忽略、历史原始字节和哈希、Codex RPC 分页/恢复/拒绝权限请求/超时回收。
- Python wheel 隔离构建通过；运行不需要第三方 Python 包。版本元数据与模块版本一致。
- [GitHub Actions](https://github.com/FacelessAkari/agent-sync/actions) 在 Windows/macOS、Python 3.12 上执行同一零跳过入口。每次远端执行状态和日志以对应提交的 Actions 结果为准。

## 真实 Codex 接口

使用已安装的 Codex CLI 0.163.0，两个独立的测试资料目录和两个不同项目路径。模型端是仅监听 localhost 的确定性 Responses 测试服务，不发送真实用户聊天、凭证或外部模型请求。

1. 实际 app-server 创建测试线程并生成一轮固定问答；创建测试源时显式选用 paginated 历史模式。
2. 产品代码通过公开 `thread/read`、`thread/turns/list`、`thread/items/list` 分页导出并归档。
3. 将档案复制到第二份测试资料目录，产品代码通过 `thread/start` + `thread/inject_items` 恢复两条文本消息。
4. 关闭恢复进程，另启 app-server，以新 ID `thread/resume` 后开始验证轮。本机 Responses 服务实际收到的上下文同时包含原测试问题与答案。

这证明已安装版本的真实接口可跨独立资料目录恢复文本上下文并在新进程续接；不是两台物理设备的端到端验收，也不是真实模型推理验证。注入可能不创建 UI 可见历史项，不能保证再次分页导出注入内容或完整原生会话无损往返。

## 实际参与者与独立测试

生产实现和主要修复由通过 moonton-llm 调用的实际 `gpt-6-astra` 完成。主协调方负责落盘、集成、测试执行、报告和发布；测试替身的 Windows UTF-8 设置、版本和文档校对由协调方完成。

Antigravity（哈基米）通过真实 `agy` CLI 独立运行预发布的一、二期测试和复现场景。她的非提权环境为 38 项通过、1 项符号链接权限错误、1 项跳过，不能视为全量通过。她复现的命名空间重叠、孤立绑定、autocrlf、缓存、Git 环境污染和交接分支问题均已进入修复与最终回归；严格真实路径、NFC 名称和 exact remote identity 作为明确契约保留。

DeepSeek Harness（鲸鲸）通过真实 headless 会话独立执行：10/10 CLI 场景、4/4 Git 预检，以及 localhost HTTP Git 双 clone 的 22/24 检查。两项非通过均指向同一个 bootstrap 忽略规则缺陷，已修复并覆盖回归。她的三期离线测试为 16/16；项目/交接 smoke 在测试 ACL 适配后通过。她的完整套件受 Windows 沙箱 tempfile/Git 限制，不能计为全量通过。最终产品传输允许 HTTPS/SSH/file；该 HTTP 服务只用于预发布独立测试，未开放为产品同步协议。

两位均核查了本机 CLI/已安装文档与接口：AGY 和 DSH 的按 ID 续接依赖已有本机会话，未找到受支持的外部历史导入及跨 Windows/macOS 同 ID 迁移接口。因此当前产品提供归档与可读 Markdown 交接，不直接改写两者内部数据库或会话文件。

## 验证范围

GitHub 项目仅保存程序、测试和文档。用户实际规则、技能、人设、长期记忆及聊天内容应放入另建的私人内容仓库；本次未自动登记、上传或覆盖这些个人资料。工具规则热加载、用户真实两台设备的实际配置，以及 AGY/DSH 原生跨端续聊不包含在已通过声明中。
