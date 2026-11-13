# AgentSync 0.4.0 架构与契约

## 仓库与状态

程序代码仓库不保存用户内容。独立私有内容仓库的 manifest v1 登记知识组；data 保存共享内容；bootstrap .gitignore 必须保护根目录 /.agent-sync/ 并忽略 __pycache__/、.DS_Store、*.pyc；内容 .gitattributes 必须为 `* -text` 加 LF，区别于源码仓库 text=auto eol=lf。两者均需手工提交，HEAD/upstream 检查以及有效属性检查在自动内容暂存前完成。本机绑定、共同基线、备份、设备身份、恢复映射和操作锁均在 .agent-sync，不进入 Git。各代码项目在自己的 Git 工作树传输，不复制 .git。

core 是底层路径、字节 I/O、锁和 manifest 校验层，不导入 records/history。data/projects、tasks、task-events、handoffs、history 是保留命名空间，知识组不能与其大小写变体、子目录交叠。所有 manifest 消费者复用核心校验；历史创建也校验已有 manifest。

## 知识同步

add/bind 只登记，plan 只比较；apply push 为绑定到内容工作树，pull 为反向。按 SHA-256 字节比较，不转换换行或编码。基线绑定本机路径和仓库 path/kind；身份改变按首次同步处理。合法孤立绑定/基线不锁死 _load；plan 显示 orphan 和 unbind 清理指令，apply 不使用或覆盖孤立状态。unbind 仅清除本机状态，remove 另清 manifest 登记，均不删除数据。树扫描在访问前跳过缓存名称，不跟随忽略链接；Git 暂存显式扫描文件列表，拒绝已跟踪缓存。任一冲突阻止整批写入；不传播删除、不提供强制覆盖。覆盖前备份，正常异常尝试回滚；多文件操作不是断电原子事务。

拒绝祖先及目标 symlink、junction/reparse point、硬链接文件、特殊文件，检查跨平台名称及允许类型。锁只约束本程序，不防御有权限的并发编辑或恶意替换；运行时停止相关编辑和 Git 操作。

## 本机快捷入口与私有目标

公开程序、资料仓库和本机工作区互不嵌套。collector 只读显式选择的真实技能源，允许源为普通硬链接，输出独立普通副本；不自动扫描主目录或写回原技能。local_bridge 在私有核验后持有工作区 quick-sync.lock，贯穿选定来源刷新、收集、计划及上传；已有锁返回 busy，不自动删除。只读检查不创建锁、不刷新对话来源。

无变化路径仍核对实际内容、绑定摘要、Git 状态和远端 HEAD；单次读取按 inode 去重，不使用跨运行 mtime 捷径。原始技能、普通镜像、数据仓库与基线一致时可跳过完整 plan/apply/git-sync。远端新提交、已有待推提交或镜像独立修改需人工处理。直接绑定的已有资料改动可通过 core 应用；技能原件不自动安装。

private_remote 在快捷同步、入口生成与所有内容 git-sync 上传前核验目标。网络目标限 GitHub，固定官方 API 核验 private=true 和仓库身份，拒绝重定向、多个目标、不同 push URL 或地址重写。令牌来自环境或现有非交互 Git 凭证管理器，仅存内存；失败即停止，不改变仓库可见性。本地绝对路径/file Git 保留离线能力，UNC 与其他网络远端不视为私有资料目标。项目代码同步仍使用其独立项目仓库契约。

shortcut 仅生成本机入口和缺失的空选择模板，先检查目标私有性及路径隔离；既有文件内容不同则拒绝覆盖。入口保存当前设备 Python、Git、程序位置与本机工作区路径，不含用户资料或凭证。scheduler 调用同一 local_bridge 管线，来源刷新只处理本机显式来源清单。

## Git 与共享记录

内容 git-sync：本地完整验证 → Git 状态检查 → HEAD 提交树检查 → fetch → upstream 提交树检查 → 暂存/提交 manifest.json 和 data/ → merge → 再次完整验证 → 非 force push。HEAD/upstream 均须跟踪 bootstrap .gitignore，明确包含独立行 /.agent-sync/，其后不得有否定规则。全局 excludes、info/exclude 或未提交工作树文件不是 bootstrap 证明；程序不代提交 bootstrap。

拒绝预先暂存、未完成 Git 操作、detached HEAD、已跟踪本机状态或非白名单改动。合并失败尽力 abort，保留已有提交；push 失败不谎报成功。Git 配置、过滤器、凭证助手及远端仍属信任边界，不是沙箱。

项目 sync 要求登记 remote/branch 与本机配置一致且工作树干净；fetch 后只允许 fast-forward，再非 force push，不创建代码提交、reset、stash 或 rebase。project status 只读返回实际 branch/head/clean、registered_branch 和本机可 sync 原因；sync 的严格匹配不放松，远端字符串不做 HTTPS/SSH 等价转换。任务快照必须匹配不可变事件，交接 JSON 必须匹配派生 Markdown 和引用任务历史；交接保存 branch/registered_branch，也接受仅含 head/clean 的记录。任务与交接文件输入统一 CRLF/CR 为 LF，历史档案的原始字节不变。交接 staging 使用自有目录下 UUID + 普通 mkdir 继承 ACL，不 chmod，保持同 FS 重命名发布和失败清理。验证文字只代表用户输入。

records.validate_shared 由调用者持锁，内部不取锁：检查 manifest、历史包目录/原始输入/record/派生 Markdown 的完整性和哈希，再检查项目任务交接。无 history 目录是正常空集合。合并后的语义错误阻止 push，保留现场供人工检查。

## 历史与 RPC

主入口将全局 --repo 和 history 参数列表委派 history.main，不修改 sys.argv；独立模块入口保留。档案 schema 为 agent-sync.history/v1，每个 UUID 包含 record.json 和 transcript.md，独占创建不覆盖。原始输入逐字节 base64 保存；RPC 响应保留 JSON 值而非 stdio 帧原字节。哈希检测损坏，不提供真实性签名。

Codex full-export 只表示公开可见持久化 display items 的分页归档：thread/read、turns/list、items/list 均使用显式 thread ID；检查 cursor 链、终止页、循环和限额。不保证跨分页事务快照，不等于全部 model-visible history 或 raw rollout 保真。

items/list 的 ThreadItemEntry 仅按明确 envelope 转换。userMessage 仅纯 text、空 text_elements；agentMessage 仅 text、受支持 phase 和安全空附加结构。非空引用、问题、附件、工具、推理、未知结构整条排除并计数；metadata 不恢复。兼容明确原始 Responses 消息，不递归猜测未知嵌套。所有原字段仍在原始归档。

恢复默认 plan；显式 apply 才 thread/start + 单次 inject_items，向新线程恢复 allowlist 文本上下文，不保留源 ID、不执行工具、不调用模型。注入可能不创建 UI-visible turn，因此未必出现在 UI 或后续分页导出中，不承诺无损往返。失败保留已知新 ID 和本机映射，不自动重试或删除。AGY/DSH 不实现原生迁移，使用用户导出和 Markdown 交接。AGY --conversation、DSH --session-id 仅续已有同机 workspace，未发现受支持外部聊天导入。工具入口和直接真实路径要求以 README 为准；Antigravity 只选一个规则入口，避免双注入。

RPC 使用直接 argv、shell=False、有界 I/O 与超时；拒绝服务端请求，回收直接子进程，不回显可能含凭证的 stderr。命令配置在内容仓库外，由用户信任。禁止使用私有数据库、rollout 或 unstable resume.history/path 补洞。

## 验证

`python scripts/run_tests.py` 检查 Git、真实文件/目录 symlink，并要求 unittest 零 skip。CI 为 Windows/macOS Python 3.12，无部署或额外 token。离线测试使用临时仓库和模拟 app-server；真实 provider turn、平台验证结果见 [verification.md](verification.md)。历史详细格式见 [history.md](history.md)。
