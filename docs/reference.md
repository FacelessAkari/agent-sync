# 命令与同步规则参考

首次使用请先看 [项目介绍](../README.md) 和 [新手上手指南](getting-started.md)。本页说明命令、文件布局、冲突处理和详细限制。

## 登记与本机绑定

内容仓库布局：

```text
manifest.json       共享的组名、类别、kind 和仓库相对路径
data/               共享内容
.gitattributes      内容保字节：* -text
.gitignore          忽略 /.agent-sync/、__pycache__/、.DS_Store、*.pyc
.agent-sync/        本机绑定、共同基线、备份与操作锁；不得提交
```

类别为 `rules`、`skills`、`personas`、`memory`，仅用于组织，不触发规则拼接或记忆注入。`file` 映射单文件，`tree` 递归映射目录内允许的文件，不同步空目录。

```sh
python -m agent_sync --repo CONTENT add shared-skills --category skills --kind tree --path data/skills
python -m agent_sync --repo CONTENT bind shared-skills /absolute/demo/skills
python -m agent_sync --repo CONTENT add knowledge --category memory --kind tree --path data/knowledge
python -m agent_sync --repo CONTENT bind knowledge /absolute/demo/AgentSyncKnowledge
```

仓库路径必须位于 `data/` 下；本机路径必须是绝对路径或 `~/...`，不能含 `..`。环境变量由 shell 展开。组名以小写字母开头，只含小写字母、数字、下划线和短横线，最长 64 字符。

每个组在每个内容工作树中只有一个本机绑定。仓库组路径不得重叠，本机绑定也不得互相重叠或与内容仓库重叠。不需要的组保持 `unbound`。克隆仓库后运行 `init`，再设置该设备的绑定。

`unbind NAME` 只清除本机绑定和该组基线；`remove NAME` 还移除 manifest 登记。两者绝不删除本机或仓库数据文件。跨端删除/改名后，旧设备的合法孤立状态不会锁死仓库：`plan` 返回 `orphan` 和 `unbind NAME` 清理提示；apply 不复用或覆盖孤立基线。同名重新 add 前须显式 unbind。

## 同步与冲突

- `push`：本机绑定 → 内容工作树。
- `pull`：内容工作树 → 本机绑定。
- `git-sync`：内容工作树与 Git upstream 之间传输；不会同步本机绑定。

先看 `plan`，再执行所需方向：

```sh
python -m agent_sync --repo CONTENT plan
python -m agent_sync --repo CONTENT apply --direction push
python -m agent_sync --repo CONTENT git-sync
```

多设备工作流程：

1. 新设备先 `git clone PRIVATE_REMOTE CONTENT`，然后 `init` 并逐个 `bind` 本设备路径，绝不复制其他设备的 `.agent-sync/`。
2. 开始工作前拉取内容：干净工作树可手工 `git -C CONTENT pull --ff-only`，或使用会双向同步的 `git-sync`；然后 `plan → apply --direction pull`。
3. 编辑本机知识后执行 `plan → apply --direction push → git-sync`，把审阅后的内容推到私有远端。
4. 另一设备重复拉取、计划、应用。`apply` 的 push/pull 只是本机复制方向，不是 Git 网络操作。

若两端都有改动，按计划处理，不要盲目覆盖。

| 状态 | 含义 |
| --- | --- |
| `push` / `pull` | 可沿对应方向复制 |
| `unchanged` | 两端字节相同；apply 建立或保留共同基线 |
| `conflict` | 首次两端不同，或相对基线双方都改了 |
| `deletion` | 一端删除，另一端仍为基线；不传播、不自动恢复 |
| `deleted-both` | 两端均已删除；apply 清理该基线项 |
| `unbound` | 本机未绑定，跳过 |

比较使用文件字节的 SHA-256，不使用修改时间，也不转换编码、BOM 或换行。`plan` 不更新基线，仅短暂创建操作锁。

基线绑定本机路径和仓库组的 `path/kind`。更换本机绑定会清除基线；仓库路径或类型与已记录身份不同、或旧格式基线缺少身份字段时，按首次同步处理，不复用旧哈希。两端已有不同内容时必须冲突，不会据旧基线推送覆盖。

**任何组出现冲突，整个 apply 在写入前拒绝。** 先备份、比较并审阅冲突，把确认后的相同结果放到两端，再 `plan/apply`。不提供强制覆盖或删除传播。重构 manifest 前应备份并停止相关编辑，不要通过修改 baseline 掩盖冲突。

## Git 设置

使用独立私人远端和普通 clone。凭证通过 SSH 或系统凭证管理器配置，不将 token 放进远端 URL。初次设置示例：

```sh
git -C CONTENT init -b main
git -C CONTENT config user.name "Your Name"
git -C CONTENT config user.email "your-address@example.invalid"
git -C CONTENT config core.autocrlf false
git -C CONTENT add -- manifest.json .gitignore .gitattributes
# data 中已有审阅过的文件时：
git -C CONTENT add -- data/
git -C CONTENT commit -m "Initialize private knowledge"
git -C CONTENT remote add origin PRIVATE_REMOTE
git -C CONTENT push -u origin main
```

将占位符替换为实际路径和私人远端。另一设备先 `git clone PRIVATE_REMOTE CONTENT`。内容仓库 `.gitattributes` 必须在首次 clone 前已提交，内容恰为 `* -text\n`，使 Windows `core.autocrlf=true` 的首次 checkout 也保留字节；clone 后才设置 autocrlf=false 不能修复已经改变的交接/档案。旧仓库先审查并手工提交两个 bootstrap 文件，再同步；不要通过重算哈希掩盖字节损坏。本程序代码仓库的 `.gitattributes` 使用 `text=auto eol=lf`，不得拿来替代内容仓库策略。

`git-sync` 要求已有 HEAD、标准 remote 分支 upstream，且 `--repo` 正好是工作树根。HEAD 和 fetch 后的 upstream 都必须已跟踪 `.gitignore` 和 `.gitattributes`。ignore 须包含独立行 `/.agent-sync/`、`__pycache__/`、`.DS_Store`、`*.pyc`，保守拒绝否定规则。attributes 须恰为 `* -text` 加 LF；暂存前还检查实际生效的 text/filter/ident/encoding/eol 属性，拒绝本机 info/attributes 等覆盖。不能依赖未追踪的 `.gitignore`、全局 excludes 或 `.git/info/exclude`。缺少保护时在自动暂存、内容提交和 push 前失败；程序不代用户提交 bootstrap。

它只提交 `manifest.json` 和整个 `data/`，包括其中未登记但合规的文件；`.gitignore`、`.gitattributes` 等初始化文件需提前手工提交。暂存使用扫描所得的显式文件列表，不对整个 data 目录执行 git add，缓存不会因本机忽略配置变化而误入提交。提交前及合并后验证共享记录和历史档案完整性、原始输入与派生 Markdown 哈希，损坏档案不得推送；无历史目录正常。

已有暂存改动、未完成合并、detached HEAD、非白名单已跟踪改动或被跟踪的 `.agent-sync` 都会拒绝。其他未跟踪文件不提交、不删除。执行 fetch、检查远端内容、提交本地允许的改动、merge、非强制 push；合并冲突时 abort 并保留两侧提交。失败后检查 `git status`，不假定操作已全部撤销。

## 二期：项目、任务与交接

二期不归档聊天、不读取原生会话、不自动续聊。项目代码在每个项目自己的 Git 仓库中传输，不复制项目目录或 `.git` 到内容仓库。

共享布局：

```text
data/projects/registry.json    逻辑 project id、remote URL、分支
data/tasks/<id>.json           当前任务快照
data/task-events/<id>.json     不可变变更事件，包含来源 device、前后快照
data/handoffs/<id>/record.json 不可变交接元数据与明确输入
data/handoffs/<id>/summary.md  同一交接的 Markdown 表示
.agent-sync/projects.json     本设备的 project 绝对路径
.agent-sync/identity.json     本内容工作树设备身份，随机生成，不使用机器名
```

`data/projects`、`data/tasks`、`data/task-events`、`data/handoffs`、`data/history` 为保留管理目录，不得与知识同步组交叠，包括大小写变体和子目录。核心 manifest 校验统一拒绝非法组；add、apply、历史创建及共享验证均受约束。不要手工覆盖事件或交接包。`.agent-sync/` 仍忽略、不提交；设备身份以本机内容工作树为范围。

### 项目

首次 clone、认证和 upstream 由用户手动设置。HTTPS URL 不得内嵌凭证、query 或 fragment；也支持 `git@host:owner/repo.git`。项目 id 使用一期组名同样的安全命名规则。

```sh
git clone git@example.com:owner/demo.git /absolute/projects/demo
python -m agent_sync --repo CONTENT project add demo --remote git@example.com:owner/demo.git --branch main
python -m agent_sync --repo CONTENT project bind demo /absolute/projects/demo
python -m agent_sync --repo CONTENT project list
python -m agent_sync --repo CONTENT project status demo
python -m agent_sync --repo CONTENT project sync demo
```

另一设备先同步内容仓库、运行 `init`，再手动 clone 项目并执行 `project bind demo`，可以使用完全不同的本机绝对路径。`project list` 输出中的 `path` 只用于本机终端，不写入共享记录。项目绑定要求已有普通 Git 工作树根目录和已提交 HEAD，不支持 bare 根目录、linked worktree 或子模块的 `.git` 文件布局。

`project status` 是只读本机诊断，feature branch/detached 也返回实际 snapshot、registered_branch、can_sync 和 reasons；can_sync 仅表示本机预检，不代表已验证远端分叉或网络。`project sync` 要求本地分支名、标准 upstream、remote 的原始及 Git 展开后 fetch/push URL 都与登记完全相符；多 URL、不同 push URL、mirror remote、进行中的 Git 操作、detached HEAD、暂存或未暂存改动、未跟踪文件均拒绝。忽略文件不计入 Git dirty 状态。

执行顺序为 fetch 指定分支 → 检查双边分叉 → `merge --ff-only --no-autostash` → 非 force push。显式指定传输 refspec，不采用 remote 中额外配置的 fetch/push refspec，不自动创建提交、reset、stash 或 rebase。已有明确本地提交可以推送；双边分叉需用户手工处理。fetch 或 merge 已成功而 push 失败时不回退已完成步骤，检查状态后重试。运行期间停止编辑及其他 Git 操作；本地锁不约束编辑器或手工 Git。

Win/Mac 必须使用完全相同的登记远端字符串；不自动互换 HTTPS/SSH，也不将不同 URL 判为同一仓库。内容与项目 Git 传输仅允许 https/ssh/file，不因 localhost 测试开放 http。环境中的 Git 仓库、索引、对象目录和 CONFIG_PARAMETERS/COUNT/KEY_*/VALUE_* 注入会被剥离，本机凭证助手保留。也接受绝对本地 bare 路径，**仅用于离线测试，不是跨设备远端方案**。没有自动 clone 子命令。项目 Git hooks 在自动操作期间禁用；本机 Git 配置、凭证助手和过滤器仍是信任边界，不是沙箱。

### 任务

`TASK_ID` 替换为 `task add` 返回的 id；也可通过 `--id` 指定安全 id。`--note-file` 为显式指定的本地 UTF-8 `.txt` 或 `.md` 文件，绝对路径或 `~/` 路径，不含 `..`；只读，拒绝链接、硬链接、特殊文件和大于 1 MiB 的输入。

```sh
python -m agent_sync --repo CONTENT task add --project demo --title "完成同步验收" --note-file /absolute/inputs/notes.txt
python -m agent_sync --repo CONTENT task list --project demo
python -m agent_sync --repo CONTENT task show TASK_ID
python -m agent_sync --repo CONTENT task set TASK_ID --status doing
python -m agent_sync --repo CONTENT task set TASK_ID --status blocked --note-file /absolute/inputs/blockers.txt
python -m agent_sync --repo CONTENT task set TASK_ID --status done
python -m agent_sync --repo CONTENT git-sync
```

状态仅为 `todo/doing/blocked/done`。每次 add/set 写入独立随机事件文件，再更新单任务 JSON；事件保存前后快照、随机 device id 与 UTC 时间，不依赖 mtime 判定覆盖。事件和任务由内容仓库 `git-sync` 传输；冲突不自动选边。快照必须与其引用事件相符，Git 文本合并产生的不一致会在 push 前报错，保留提交供手工检查。

### 交接

四项输入均必须明确提供：`--summary-file` 写完成情况，`--next-file` 写下一步，`--blockers-file` 写阻塞，`--verification-file` 写实际验证结果。没有阻塞或尚未验证时请明确写“无”或“未验证”，不由程序推断通过。

```sh
python -m agent_sync --repo CONTENT handoff create --task TASK_ID --summary-file /absolute/inputs/completed.txt --next-file /absolute/inputs/next.txt --blockers-file /absolute/inputs/blockers.txt --verification-file /absolute/inputs/verification.txt
python -m agent_sync --repo CONTENT handoff list --task TASK_ID
python -m agent_sync --repo CONTENT handoff show HANDOFF_ID
python -m agent_sync --repo CONTENT git-sync
```

交接在本机必须已绑定相应项目，只读取得当前已提交 HEAD、Git 工作树是否干净、实际 branch（detached 为 null）及 registered_branch；兼容旧的仅 head/clean 档案；不要求已联网或验证通过。JSON 与 Markdown 一起发布，API 不提供修改、覆盖或删除交接的命令。记录不包含输入文件名、绑定路径、真实机器名，也不自动读取聊天或其他外部文件。**二期输入文件的 CRLF/CR 统一为 LF 后共享，用户必须先排除其中的路径、密钥和私人内容。** 此规范化不改变三期历史归档的原始输入字节。 记录中的验证文字是用户提供的陈述，不是 AgentSync 执行测试的证明。

任务事件先落盘、快照后落盘不是跨文件断电事务；中断可能留下尚未应用的事件，可据事件和 Git 历史人工恢复，不应删除证据。交接在 `.agent-sync/handoff-staging/` 下以 UUID 和普通 mkdir 独占创建目录，继承 ACL/umask，不 chmod、不修改系统权限；同文件系统内重命名发布并清理失败暂存目录；中断前的临时目录可以在确认无运行实例后清理。不可变性是本 CLI 的写入约束，不是文件系统 WORM 或防篡改签名。

## 三期：可见历史归档与文本上下文恢复

两个稳定入口等价：`python -m agent_sync --repo CONTENT history ...` 与 `python -m agent_sync.history --repo CONTENT ...`。`--max-bytes` 放在 history 的动作之前。完整格式、命令配置与限制见 [历史详解](history.md)。

```sh
python -m agent_sync --repo CONTENT history capability
python -m agent_sync --repo CONTENT history codex-export --thread-id SOURCE_THREAD_ID
python -m agent_sync --repo CONTENT history list
python -m agent_sync --repo CONTENT history verify ARCHIVE_ID
python -m agent_sync --repo CONTENT history codex-restore ARCHIVE_ID --cwd /absolute/projects/demo
python -m agent_sync --repo CONTENT history codex-restore ARCHIVE_ID --cwd /absolute/projects/demo --apply
python -m agent_sync --repo CONTENT git-sync
```

Codex 导出只读取显式 ID 的公开 metadata、turns/items 分页。`full-export` 表示公开接口可见的持久化 display items 分页归档，不是全部 model-visible history，也不是 raw rollout 完整保真。归档保留返回 JSON 的所有字段；恢复仅重建安全 allowlist 内的 user/assistant 文本消息，不重放工具、引用结构、附件、内部推理或权限指令。默认恢复只生成计划；`--apply` 新建线程后单次注入，不调用模型、不保留源 ID。

**注入成功不保证创建 UI-visible turn。文本上下文未必出现在 UI 历史，可能不在再次分页导出中出现；不承诺无损往返或完整原生迁移。** 不使用私有数据库、rollout 文件或 unstable resume.history/path 补齐不可见历史。

AGY/DSH 只支持用户提供的 transcript/UTF-8 文本归档及 Markdown 交接；AGY 单次结果 JSON 永远是 partial，不视为完整会话。AGY 的 `--conversation` 和 DSH 的 `--session-id` 只是续已有同机 workspace，未发现受支持的外部聊天导入接口，不能当作跨设备导入。没有已实现的 AGY/DSH 原生跨设备迁移：

```sh
python -m agent_sync --repo CONTENT history import --format text --tool dsh --input conversation.txt
python -m agent_sync --repo CONTENT history import --format agy-result --input agy-result.json
python -m agent_sync --repo CONTENT history export ARCHIVE_ID --kind markdown --output handoff.md
```

将审阅后的 `handoff.md` 手工提供给目标工具的新会话。每份档案在 `data/history/<UUID>/` 独占创建 `record.json` 与 `transcript.md`，不覆盖旧档案；原始输入逐字节保存，哈希检测损坏而非提供签名。恢复映射只保存在本机 `.agent-sync/history-restores.json`。来源可能包含路径或敏感内容，同步前必须审阅。

## 工具路径与独立人设

路径由 `bind` 明确配置，没有自动探测适配器。同步后需让工具重新读取规则或新建会话，并核对实际读取来源，不承诺热加载。

| 工具 | 规则路径 | 技能路径与注意事项 |
| --- | --- | --- |
| Codex | 默认示例 `~/.codex/AGENTS.md`；项目级 `AGENTS.md` 按工具优先级读取 | 显式核对当前版本技能目录后 bind，不绑定整个工具 home |
| DeepSeek DSH | 默认 profile 示例 `~/.dsh/AGENTS.md`；自定义 DSH_HOME 时使用该 profile 的 AGENTS.md | 多 profile 分别绑定，升级后复核实际读取来源 |
| Antigravity | 本文只选 `~/.gemini/config/rules/AGENTS.md` 作为规则入口 | 避免在 `~/.gemini/AGENTS.md` 同时放置重复规则；安装版本须核对实际读取来源 |

学姐、哈基米、鲸鲸的人设分别登记，不自动合成。鱼鱼也保持独立。鲸鲸与鱼鱼不能同时写入同一个 DSH 规则文件，可分别指定 `DSH_HOME=~/.dsh-whale` 和 `DSH_HOME=~/.dsh-fish`；这仅说明规则路径选择，不承诺其他内部状态或技能目录全面隔离。DSH 路径依据已提供的 `0.1.0-rc.6` 本机核实信息，升级后应复核。

公共知识可绑定到约定目录 `~/AgentSyncKnowledge`，各人设规则分别引用。可使用以下指引：

```text
保持本 profile 指定的人设，不替换或合并其他人设。
读取当前用户主目录下 AgentSyncKnowledge/common-rules.md、
preferences.md 和 memory/index.md，再按索引读取相关事实。
读不到时说明缺失，不得假装已加载。
按当前用户授权记录已核实事实；已有持续授权的事项可持续执行，
无需每次重复申请。遇到冲突、事实不确定或超出授权范围时提交审阅。
```

这是要求助手读取普通文件，不是注入工具内部记忆。不要绑定整个 `.codex`、`.dsh` 或 `.gemini`；原生数据库、会话目录、`brain`、`memories_v2` 和 `rollout_summaries` 不作为共享记忆。

## 边界与恢复

允许扩展名（不区分大小写）：

```text
.md .txt .json .yaml .yml .toml
.py .ps1 .sh .js .mjs .cjs .ts .jsx .tsx
.html .htm .xml
.png .jpg .jpeg .webp .svg .gif
```

树扫描按精确名称忽略 `__pycache__` 缓存项、`.DS_Store` 和 `*.pyc`，在读取/跟随链接前跳过，不进入计划文件项或 baseline；缓存目录内容不审阅、不传输。已被 Git 跟踪的缓存会拒绝，需手工从索引移除而保留本机文件。`auth.md` 仅作为路径最后一段的普通文档文件允许（不区分大小写）；同名目录及其他 `auth.*` 名称仍拒绝。`.env`、凭证、密钥和运行时名称拒绝；这不是自动 secret scanner。脚本、网页、组件、XML 和图片只作为字节复制，不执行、渲染或验证其功能。其他类型仍拒绝。整个 `data/` 都需通过检查；点开头、敏感和运行时名称、越界路径、Windows 保留名称、大小写碰撞及非 NFC 名称仍被拒绝。

生产路径检查拒绝符号链接、junction/reparse point（包括祖先）、硬链接文件和特殊文件，不跟随目录链接。使用直接真实目录，不宣称支持挂载目录、云占位符或链接绕过。macOS 临时根应先由用户/测试夹具取得 realpath 后的 `/private/...` 路径，再传给程序；生产校验不会自动 resolve 来绕过链接。名称必须为 NFC。只同步字节，不同步权限、ACL、资源分叉或可执行位。白名单不是密钥扫描器，也不是恶意脚本或指令检测器；提交前需审阅内容。

覆盖前的旧字节与事务索引位于 `.agent-sync/backups/`。文件采用同目录临时文件替换，正常异常会尝试回滚；若目标又被外部修改，则拒绝回滚覆盖并提示人工恢复。备份含内容和本机路径，不加密、不自动清理。

这不是跨文件崩溃原子事务，也不抵御有权限的恶意进程替换路径。运行时应停止相关编辑和并行 Git 操作；锁只约束 AgentSync。断电或强制终止后可能需要备份恢复。Git 配置、凭证助手、过滤器和远端属于信任边界，不是沙箱。

## 测试

在代码目录运行：

```sh
python -m unittest discover -s tests -v
```

发布及 CI 入口：

```sh
python scripts/run_tests.py
```

脚本要求 Git 和真实文件、目录 symlink 权限，执行完整 unittest；任何失败、错误或 skipped>0 都返回失败。Windows 使用具备权限的测试进程，必要时在管理员环境执行，不以 skip 代替安全测试。GitHub Actions 在 Windows/macOS、Python 3.12 上使用相同入口。

测试使用临时目录、本地 bare remote、隔离的 Git 配置和 Python 模拟 app-server，不访问真实聊天、凭证或网络 provider。仅测试夹具解析可信临时根的真实路径，生产路径检查不扩大信任。执行结果及独立真实接口验证见 [验证记录](verification.md)。
