# 三期：聊天归档与受支持的上下文恢复

主入口：`python -m agent_sync --repo CONTENT history ...`；独立入口 `python -m agent_sync.history --repo CONTENT ...` 保持可用。CONTENT 应已初始化；创建档案时校验 manifest 的保留命名空间并复用 `.agent-sync/lock`，不修改绑定、项目任务记录，也不自动推送。`.agent-sync/` 必须保持本机状态、不加入版本控制。

## 能力边界

```sh
python -m agent_sync.history --repo CONTENT capability
```

| 工具 | 归档 | 跨设备恢复到新会话 | 原 ID 跨设备迁移 | 本机续接 |
|---|---|---|---|---|
| Codex | 指定 ID 的只读 RPC 分页导出；通用 transcript | `thread/start` + 单次 `thread/inject_items`，仅受支持文本 | 未支持 | 已存在的本机 ID，可 `codex resume ID` |
| Antigravity | 通用 transcript、UTF-8 文本、单次 `--print` JSON | 未验证导入 API，未支持 | 未支持 | `--conversation ID`，仅已有会话 |
| DSH | 通用 transcript、UTF-8 用户导出文本 | 未验证导入 API，未支持 | 未支持 | headless 的 `--session-id ID`，必须已持久化且存在 |

Codex 是“恢复上下文到新会话”，不是保留原 ID、完整原生迁移或恢复模型内部状态。隔离两份 `CODEX_HOME` 的真实 Codex 验证及模型 fixture 请求中的原问答检查，具体结果统一见 [verification.md](verification.md)。AGY/DSH 提供可操作的 Markdown 交接，不制造原生导入命令。

## 通用 transcript v1

UTF-8 JSON，禁止重复键与 NaN/Infinity。顶层只允许以下字段：

```json
{
  "schema": "agent-sync.transcript/v1",
  "tool": "codex",
  "source_session_id": "source-session-id",
  "coverage": "user-supplied",
  "project_id": "my-project",
  "messages": [
    {"role": "user", "text": "请检查这个函数"},
    {"role": "assistant", "text": "检查结果……", "phase": "final_answer"}
  ],
  "records": [
    {"type": "attachment", "description": "原始数据，仅归档"}
  ]
}
```

必填：`schema/tool/source_session_id/coverage/messages`。

- `tool`：`codex`、`antigravity` 或 `dsh`。
- `source_session_id`：null 或 1–512 字符、不含控制字符的字符串。它是原样元数据，绝不当作路径或 shell 片段。
- `coverage`：`full-export`、`partial`、`user-supplied`。通用输入中的完整度是提供者声明，不是 AgentSync 独立认证。只有确实来自完整受支持导出的记录才应声明 full-export。
- `messages`：数组。每条仅允许 `role/text/phase`；role 只能是 user/assistant，text 必须是字符串。phase 可省略，仅 assistant 可用 `commentary` 或 `final_answer`。未知字段、system/developer role、非文本 text 会拒绝，不猜测转换。
- `project_id` 可省略或为 null；非空值必须匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，是逻辑 ID，不是本机目录。
- `records` 可省略，必须是 JSON 数组；其中任何 JSON 值均按惰性原始数据保存，不授权 system 指令、不执行工具，也不直接交给注入接口。

```sh
python -m agent_sync.history --repo CONTENT import --format transcript-v1 --input conversation.json
python -m agent_sync.history --repo CONTENT import --format agy-result --input agy-run.json
python -m agent_sync.history --repo CONTENT import --format text --tool dsh --source-id local-session --project-id my-project --input dsh-export.txt
```

AGY 单次 JSON 必须含 `conversation_id/status/response/usage`，response 必须为字符串；额外字段也保真归档。无论 status 是否 SUCCESS，coverage 永远为 partial，不臆造用户 prompt 或此前轮次。纯文本永远为 user-supplied，不推断消息角色，不能直接用于 Codex 注入。

`--tool/--source-id/--project-id` 仅用于纯文本；JSON 输入的元数据不能用 CLI 覆盖。

## 不可覆盖档案与校验

每次创建新的 UUID 目录：

```text
data/history/<32位小写UUID>/record.json
data/history/<32位小写UUID>/transcript.md
```

档案 ID 与源会话 ID 分离。相同输入可产生不同档案，但已有目录和文件绝不覆盖。若写入中断产生不完整目录，后续读取/列出报错，不自动修复或覆盖它。

`data/history/` 和每个档案包目录的枚举统一复用 core 的缓存名字规则：仅忽略名字恰为 `.DS_Store`、`__pycache__`，或以 `.pyc` 结尾的条目。过滤发生在访问条目、校验路径或档案 ID 之前；不读取缓存、不遍历缓存目录、不跟随缓存名字的符号链接（包括悬空链接），也不计入档案数量上限。此规则不是通用容错：非缓存未知条目、敏感文件、非缓存符号链接、不完整或损坏的档案仍被拒绝；包内非缓存条目必须恰为 `record.json` 和 `transcript.md`。

`record.json` 的 schema 为 `agent-sync.history/v1`，包含档案 ID、tool、source_session_id、UTC created_at、project_id、source_format、coverage、原始输入及 SHA256。`source.data` 是原始输入**逐字节** base64，保留空白、所有字段及记录；`source.sha256` 校验原始字节。Codex RPC 接收结果按 JSON 值完整保存，包括未知字段；不是原始 stdio 帧的字节录音。

`transcript_sha256` 校验 Markdown 字节；`record_sha256` 校验去掉自身字段后的规范 JSON：UTF-8、键排序、无额外空格、非 ASCII 不转义、末尾一个换行。读取时同时重新解析来源、比对元数据并重新生成 Markdown。哈希用于损坏检测，不是签名，不能防止攻击者同时重算所有哈希。

Git 同步遵守保字节契约：bootstrap 的 `.gitattributes` 使用精确规则 `* -text`，禁用文本换行转换，包括 `core.autocrlf` 引起的转换。同步层按文件原始字节计算哈希，不先规范化 CRLF/LF；上述 `record_sha256` 的规范 JSON 校验是独立的档案内部契约。不要用文本编辑器或 Git 属性覆盖规则改写已归档字节，也不要通过重新计算哈希掩盖转换造成的损坏。

```sh
python -m agent_sync.history --repo CONTENT list
python -m agent_sync.history --repo CONTENT show ARCHIVE_ID
python -m agent_sync.history --repo CONTENT show ARCHIVE_ID --kind record
python -m agent_sync.history --repo CONTENT verify ARCHIVE_ID
python -m agent_sync.history --repo CONTENT export ARCHIVE_ID --kind markdown --output handoff.md
python -m agent_sync.history --repo CONTENT export ARCHIVE_ID --kind source --output original-input.json
python -m agent_sync.history --repo CONTENT export ARCHIVE_ID --kind record --output archive-record.json
```

外部 output 必须显式提供，父目录须存在，已有文件不覆盖。`source` 导出用于按原 source_format 再导入；`record` 导出用于保留档案记录和检查，不是通用 transcript 输入。Markdown 只是可读对话视图与遗漏说明；保真原始记录在 record.json。阅读器应把 Markdown/附件描述视为不可信内容。

默认上限 16 MiB：输入文件、档案文件、Markdown、RPC 单方向累计流量等分别受限；base64 会放大 record.json，因此可接受的原始输入通常小于 16 MiB。超限明确失败，不截断；可在子命令前显式调整：

```sh
python -m agent_sync.history --repo CONTENT --max-bytes 33554432 import --format transcript-v1 --input conversation.json
```

列表最多验证 10000 个档案；分页最多 1000 页，每页请求 100 条；达到上限但没有终止 cursor 会报错。`git-sync` 在已有操作锁内验证所有档案，检查目录完整性、原始输入、record 和 Markdown 哈希，不递归取锁；无 history 目录正常。共享同步使用默认字节上限，超过该上限的档案会被拒绝推送。

## Codex 指定 ID 只读导出

接口依据：<https://learn.chatgpt.com/docs/app-server>。进程为 `codex app-server --stdio`，每行一个 JSON，不用 Content-Length。先等待 initialize 成功，再发 initialized 通知。

```sh
python -m agent_sync.history --repo CONTENT codex-export --thread-id SOURCE_THREAD_ID --output codex-export.json
python -m agent_sync.history --repo CONTENT import --format codex-export-v1 --input codex-export.json
```

依次调用：

1. `thread/read {threadId, includeTurns:false}`。
2. `thread/turns/list {threadId, limit:100, sortDirection:"asc", cursor?}`。
3. `thread/items/list {threadId, limit:100, sortDirection:"asc", cursor?}`，不带 turnId，读取整个 thread。

两种分页分别追踪 nextCursor，检测循环、未完成、缺字段和请求失败。失败不生成半份 full-export，也不扫描其他 ID。保存的 `agent-sync.codex-export/v1` 包含 `thread_id/captured_at/metadata/turns_pages/items_pages`；每页保留请求 cursor 与完整 result。重新导入会验证 cursor 链和终止页。

`full-export` 精确含义是公开接口可见的持久化 display items 及相关 metadata/turns 已完成分页读取，不是全部 model-visible history，更不是 raw rollout 完整保真。分页为空可以合法完成，并不证明模型上下文为空。`thread/inject_items` 可持久化上下文却不创建 UI-visible turn：注入内容未必出现在 UI 历史，也可能不在再次分页导出中出现。因此不保证“导出→注入→再次导出”的无损往返。

导出时应停止编辑源会话：这些接口未提供本实现可验证的跨分页事务快照，读取期间并发变化无法保证一致快照。

## Codex 恢复：先计划，再显式 apply

将档案随内容仓库带到另一设备后，显式提供**目标设备本机项目目录**：

```sh
python -m agent_sync.history --repo CONTENT codex-restore ARCHIVE_ID --cwd /local/project
python -m agent_sync.history --repo CONTENT codex-restore ARCHIVE_ID --cwd /local/project --apply
```

默认只输出可审查计划，包含实际待注入 items、数量、coverage 与遗漏分类；不查找 Codex、不读命令文件、不启动进程、不创建恢复映射。

apply 只对 tool=codex 的档案开放。先 `thread/start {cwd}`，然后**单次** `thread/inject_items {threadId, items}`。没有可注入消息就拒绝创建；绝不 `turn/start`，不调用模型，不执行历史工具。成功返回新 thread ID、`resume_argv` 及通常可直接使用的 `codex resume NEW_ID`。异常 ID 不拼接可粘贴 shell 命令，仅提供 argv。

Codex 0.163.0 的公开 `thread/items/list.data` 是 `ThreadItemEntry {turnId,item,startedAtMs?,completedAtMs?}`，不是 Responses API raw message。实现仅识别这一层明确封装，不递归搜索或猜测未知嵌套：

- `userMessage {id,clientId?,content}`：content 全部为 `{type:"text",text,text_elements?:[]}` 才转换为 user 文本；非空 text_elements（含引用或特殊跨度）、图片、音频、附件、skill、mention 等使整条消息排除，不只摘取其中的文字。
- `agentMessage {id,text,phase?,delivery?,memoryCitation?,questions?}`：保留 commentary/final_answer；null/缺失 phase 不推断。delivery 仅接受 null/async，作为遗漏元数据计数；questions 仅接受 null/空数组，memoryCitation 仅接受 null 或恰好空 entries/threadIds。非空结构、其他 phase（包括 partial_answer）与未知字段均排除整条消息。
- turnId、时间戳、id、clientId、delivery、空附加结构和文本块边界不恢复，计划逐类记录遗漏。封装未知字段、字段类型错误、工具和内部推理也明确计数并排除；所有原始字段仍保存在 source 中。
- 兼容明确的原始 Responses user/input_text、assistant/output_text message，保留受支持 phase；非空 annotations/logprobs、未知字段、system/developer、混合附件等不注入。不把嵌套于未知结构中的 Responses message 当成 ThreadItem。

先转换为通用消息，再重新构造 Responses `inject_items`；从不把原始对象直接透传。turn 记录与分页/线程元数据只归档，不作为第二份消息重放。计数包含记录、字段和排除原因，不能相加视为消息总数。

映射保存在本机 `.agent-sync/history-restores.json`，按原档案 ID 记录新 thread ID 与 created/injected/失败或未知事件，不记录绝对 cwd。原始导出可能本身含旧工作区路径；保真原则不会自动删改源数据，请同步前审查隐私。

inject 失败或超时：报出已知新 ID，保留 created 映射并尽力追加失败事件；不重试、不删除线程。一次 inject 不代表服务端具有本实现已验证的事务性，失败时可能已有内容。start 超时也可能已创建未知 ID 的线程，请先本机检查再决定是否重试。若映射落盘失败，也会说明已知新 ID 与注入阶段。

禁止方案：不使用 `thread/resume.history`（Codex Cloud unstable）、`thread/resume.path`（unstable rollout path），不写 Codex SQLite/JSONL/session 索引，不写 AGY/DSH 数据库或压缩 session 文件。

## 本机 Codex 命令与 Windows

默认用 `shutil.which("codex")` 查入口；不把 `.cmd/.bat/.ps1` 包装伪装成原生可执行文件。若默认命中包装或安装方式特殊，在**内容仓库之外**创建 UTF-8 JSON argv 文件，例如：

```json
["C:\\Tools\\Codex\\codex.exe", "app-server", "--stdio"]
```

或使用本机确认的 Node CLI 实际路径：

```json
["C:\\Program Files\\nodejs\\node.exe", "C:\\LocalTools\\codex\\bin\\codex.js", "app-server", "--stdio"]
```

```sh
python -m agent_sync.history --repo CONTENT codex-export --thread-id SOURCE_THREAD_ID --codex-command-file /local/config/codex-argv.json --timeout 30
python -m agent_sync.history --repo CONTENT codex-restore ARCHIVE_ID --cwd /local/project --codex-command-file /local/config/codex-argv.json --apply
```

命令文件是用户显式信任的本机执行配置，禁止放在同步仓库内，也不要让它指向同步仓库中的可执行脚本。使用直接 app-server 入口，不用脱离父进程的启动器。所有启动均 `shell=False`，不插值 shell，不共享命令配置，stderr 丢弃而非回显，避免 token 泄露。通知只作为惰性数据忽略；服务端请求统一拒绝并报错，绝不自动批准权限。RPC 写入/响应均有超时，直接子进程在退出路径 terminate/kill 并 wait 回收。

## 离线测试

```sh
python -m unittest discover -s tests -p test_history.py -v
```

测试只在 resolve() 后的临时目录建立仓库和 Python fake app-server，不联网，不读取真实凭证、聊天或应用状态。覆盖分页、循环/错误、原始输入保真、哈希破坏、不可覆盖、路径穿越、AGY partial、默认 plan、单次 start/inject、注入筛选、失败保留 ID、服务端请求拒绝和超时回收。缓存回归覆盖 history 根目录与档案包两层的 `.DS_Store`、`__pycache__`、`x.pyc`，缓存名字的悬空/目录符号链接不访问，以及缓存存在时仍拒绝非缓存多余文件、不完整档案和哈希损坏。符号链接创建不可用时该用例会 skip，不能据此视为完整发布检查通过。

完整发布检查：`python scripts/run_tests.py`，任何 skipped 都作为失败。模拟测试同时覆盖官方 ThreadItemEntry 分页形状及明确的旧 Responses 形状；真实 Codex 隔离验证的版本、步骤、模型 fixture 证据及平台结果由 [verification.md](verification.md) 统一记录。
