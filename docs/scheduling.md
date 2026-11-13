# 当前用户周期同步：最终设计

## 使用方式

程序仓库可以公开；同步数据仓库必须是真正的私有 GitHub 仓库。调度器不复制凭据、不保存密码，也不自行实现网络认证。安装前调用 `private_remote.assert_private_repo(repo)`，返回的元数据不会写入登记文件。每次实际同步仍由 `local_bridge.run` 执行自己的私有远端检查。

在需要自动同步的每台 Windows / macOS 设备上，由用户明确执行安装。以下 `ABS_WORKSPACE` 与 `ABS_PRIVATE_REPO` 都要替换为本机真实的绝对目录路径，包含空格时按当前终端规则加引号：

```text
python -B -m agent_sync schedule install --workspace ABS_WORKSPACE --repo ABS_PRIVATE_REPO
python -B -m agent_sync schedule install --workspace ABS_WORKSPACE --repo ABS_PRIVATE_REPO --interval-minutes 30
python -B -m agent_sync schedule status --workspace ABS_WORKSPACE --repo ABS_PRIVATE_REPO
python -B -m agent_sync schedule remove --workspace ABS_WORKSPACE --repo ABS_PRIVATE_REPO
python -B -m agent_sync schedule run --workspace ABS_WORKSPACE --repo ABS_PRIVATE_REPO
```

也可以把 `-m agent_sync schedule` 换成 `-m agent_sync.scheduler`。建议使用 `-B`，避免手动启动 Python 时在程序安装目录生成字节码；所有系统任务的固定动作均已包含 `-B`。

默认周期为 **60 分钟**，允许 `1..10080` 分钟。再次 `install` 是更改周期的方式；省略参数会恢复 60 分钟。`run` 是一次性执行，不要求先有操作系统登记，也不会自己注册任务。错误和忙碌返回退出码 2，成功返回 0。CLI 正常结果和参数错误均输出单个紧凑 JSON，并包含中文消息；标准 `--help` 输出帮助文本。

一个工作区只有一个调度槽，绑定一个数据仓库。需要另一个仓库时使用另一个独立工作区，不接管原槽。移动程序、工作区、数据仓库或 Python 环境后应先在原位置移除登记，再在新位置重新安装；调度器不会猜测迁移关系。

安装时保存当前 Git 可执行文件的真实路径。定时运行会临时将它所在目录加入本进程的 `PATH`，结束后恢复；不修改系统 `PATH`，也不保存完整环境或凭证。这样不依赖终端启动配置。记录位置的 Git 缺失、不可执行、类型异常，或该文件及其任意祖先变为符号链接/junction/reparse point 时，同步停止并在本地记录原因；在可正常找到新 Git 的终端重新安装调度。状态查询和移除仍可使用。

## 路径和初始化边界

三个目录必须两两完全独立，不能相等，也不能互为祖先或后代：

1. 程序根目录：`Path(__file__).resolve().parents[1]`；允许来自 pip 安装，不检查 `.git`。
2. 用户显式指定的本地工作区。
3. 用户显式指定的私有数据仓库。

工作区和仓库都必须已经存在且是真实目录。输入不能使用相对路径、`~`、`..` 或控制字符。除 Git 最终执行文件外，所有访问的路径通过 `core._safe` 检查，拒绝任意祖先中的 symlink、junction、reparse point，以及硬链接普通文件或特殊文件。Python 运行文件也必须是无链接的普通文件；如果当前解释器入口本身是链接，请用真实解释器路径启动安装。

Git 是用户安装时从可信 `PATH` 选定的只读执行依赖，允许其最终普通文件为硬链接。安装阶段通过 `resolve(strict=True)` 固定真实绝对路径；已登记运行不重新查找或跟随链接。父目录及全部祖先继续通过 `core._safe` 检查，最终文件通过 `lstat` 确认为非 symlink、无 reparse 属性的普通文件，并检查执行许可。程序不改写 Git 文件；此例外不适用于 Python、同步资料、调度状态、锁和日志。该检查确认路径与文件类型，不代替用户对 Git 安装来源的信任，也不保证外部依赖 DLL 可以启动。

登记要求数据仓库的 `manifest.json`、`.agent-sync/device.json` 和 `.agent-sync/baseline.json` 已存在且为普通文件。这里不遍历设备 bindings、不探测用户个人目录，也不通过读取个人内容来判定初始化。同步桥负责后续完整的同步语义验证。

机器路径、运行参数、候选定义、事务记录和日志均写在显式工作区的 `schedule/` 内。唯一的工作区外生成文件例外是 macOS 必需的当前用户 `Library/LaunchAgents/<label>.plist`。调度器只通过当前 UID 的账户记录取得这个标准安装目的地，不枚举 home，也不读取其中其他内容。该安装目的地同样不能与上述三个目录重叠。

## Windows

登记为普通当前用户的 Task Scheduler 任务，名称为 `AgentSync-<12 位摘要>`。摘要来自规范化工作区与仓库路径。当前 SID 通过无 shell 的 `whoami.exe /user /fo csv /nh` 获取，不保存账户名或命令错误输出。

XML 使用 ElementTree 生成；`candidate.xml` 和提交后的 `task.xml` 固定保存为 **UTF-16 LE 字节、开头 `FF FE` BOM、`encoding='utf-16'` 声明**，不依赖主机字节序或本地代码页，中文及其他 Unicode 路径保持原值。本地定义始终直接严格解析原始 XML 字节，兼容 UTF-8 与 UTF-16 LE、BE，不做有损解码或编码修复。WindowsBackend 对精确任务名执行 `schtasks /Query /TN <name> /XML` 得到的 stdout 可能以 ASCII、UTF-8 或本地代码页传输却声明 UTF-16；query/verify 仅对该受控命令响应按 `_text` 严格解码后解析，query 的 `raw` 保留原始字节。真正的 UTF-16 LE/BE（有或无 BOM）仍可识别。此传输规则不适用于本地 spec，不改变原始字节哈希检查。所有解析入口均在处理内部子集及实体展开前拒绝任何 DTD；无法严格解码或解析时需要检查。任务具有：

- `InteractiveToken`、`LeastPrivilege`，不请求管理员权限，不询问密码。
- `Enabled=true`、`Hidden=true`、`MultipleInstancesPolicy=IgnoreNew`。
- 允许电池运行，不因切到电池停止，`WakeToRun=false`。
- `AllowHardTerminate=false`，`ExecutionTimeLimit=PT0S`，不设置 45 分钟强杀超时。
- 单一 TimeTrigger，首次计划时间为安装时间加一个周期；重复周期使用 `PT{分钟}M`，不设置 Duration，因此不是只重复一天。
- 单一 Exec：当前解释器旁的 `pythonw.exe`，参数为 `-B -m agent_sync.scheduler run ...`，工作目录为程序根目录。没有 `pythonw.exe` 时明确失败，不回退到会弹出控制台的 Python。

动作参数使用 `subprocess.list2cmdline`，不使用 shell。首次 `/Create` 不带 `/F`；更新必须先验证本地状态、定义哈希及实际任务的语义，再对该精确名称使用 `/F`。删除也只针对验证通过的精确名称，不使用通配符。

比较忽略 XML 前缀、元素排列顺序、等价 ISO 时长和带时区起始时间的等价表示。仅下列精确完整父路径中的默认叶节点与缺省等价：

- `Task/Triggers/TimeTrigger/Repetition`：`StopAtDurationEnd=false`。
- `Task/Triggers/TimeTrigger`：`Enabled=true`。
- `Task/Principals/Principal`：`RunLevel=LeastPrivilege`。
- `Task/Settings`：`Enabled=true`、`RunOnlyIfNetworkAvailable=false`、`AllowStartOnDemand=true`、`RunOnlyIfIdle=false`、`WakeToRun=false`、`Priority=7`、`DisallowStartOnRemoteAppSession=false`、`UseUnifiedSchedulingEngine=false`、`Volatile=false`。

这些默认节点必须属于任务命名空间、无属性、无子节点，且同一父节点下不得重复。相反值、未知节点、错路径节点仍保留差异，外国命名空间直接拒绝；`Date`、`Author` 没有例外。查询省略 `Task/Settings/Enabled` 时按系统默认返回 true。动作、触发器、SID、URI、所有者标记、周期、工作目录和其他设置的差异仍受检查。额外动作或触发器、用户改动设置、未知导出格式均拒绝接管。跨编码语义相等不替代本地定义文件的原始字节 SHA-256 检查；重新编码或追加混编码内容仍属本地篡改，保留文件并拒绝覆盖或删除任务。安装命令返回 0 还不够，必须读取实际登记并验证其内容和启用状态。

单任务查询失败不等于任务不存在。调度器必须再成功枚举任务，确认精确名称缺失；无法查询或枚举时返回需要检查。XML 查询不提供可靠的本地化无关运行状态，所以 Windows 的 `running` 可以为 `null`，不通过中文/英文状态词猜测。

## macOS

登记为当前登录用户 GUI 域中的 LaunchAgent，名称为 `com.agentsync.sync.<12 位摘要>`，不是系统 daemon，不使用 sudo。plist 中使用数组形式的 `ProgramArguments`，路径中的空格、引号和 `&` 都保持为独立 argv 数据，不经过 shell。

主要字段为 `StartInterval=分钟*60`、`RunAtLoad=false`、`ProcessType=Background`、程序工作目录和所有者标记。默认间隔为 3600 秒。没有把 stdout/stderr 直接接到文件，以免原始错误或无限增长输出泄漏；调度器自身写受控运行摘要。

操作使用 `launchctl bootstrap gui/<uid> <plist>`、`launchctl print gui/<uid>/<label>` 和 `launchctl bootout gui/<uid>/<label>`。更新前核对工作区定义、外部 plist 的原始内容，以及实际登记中的 plist 路径、程序、参数、工作目录、周期和启用状态。更改周期时卸载旧服务，再写新 plist 并 bootstrap。被用户禁用、改过 plist、丢失、无法解释或同名未受管的登记都需要人工检查，不自动启用、替换或认领。

服务查询仅在返回 launchctl 的未知服务代码 113，且 GUI 域可成功查询、域响应中也不存在该名称时认定缺失；其他错误不能当作不存在。`launchctl print` 是诊断文本而非稳定 JSON，解析策略保守：无法验证的系统输出版本将失败关闭，不冒险修改任务。

macOS 安装需在当前用户会话中完成 bootstrap，并读回核对实际登记；无法识别当前系统的 print 输出时安装失败。各平台的真实验证范围见[验证记录](verification.md)，合成响应测试不能替代系统任务的实际注册与运行。

## 状态、所有权和失败事务

`state.json` 使用带校验和的版本化记录，包含平台、稳定名称、所有者标记、创建/更新时间、周期、精确运行程序、argv、工作目录、定义文件及 SHA-256、外部 plist 位置和最近一次已提交的安装状态。校验和用于检测意外或用户改动，不是针对恶意同账户用户的签名。

安装、更新、删除都先校验受管身份。定义文件缺失或被改动、系统任务被改动、外部 plist 被改动时保留现状，不覆盖、不删除。成功删除系统登记后，工作区内的状态、定义和运行记录仍保留，`installed` 记录为 false。之后可以重新安装；如果出现同名新任务则仍拒绝接管。

开始改变系统登记前先写 `pending.json`，安装还保留 `candidate.xml` 或 `candidate.plist`。只有系统操作成功、实际登记读回验证成功、本地状态提交完成之后才清除事务标记。查询读回失败、命令超时、部分失败或进程被中断都不会被标为安装成功。

这里采用**保留证据、失败关闭**，不尝试对结果未知的操作系统命令进行盲目回滚：

- Windows 更新失败时保留旧的已提交状态/定义，但实际任务是否改变可能不确定。
- macOS 更新可能已经卸载旧服务，或已写入新 plist，但 bootstrap 未完成。
- 状态提交中断也可能留下新旧文件组合。

这些情况下保留事务标记，`status` 返回 `needs_review`，后续安装、删除和运行拒绝继续。不会因重试而悄悄认领一个可能由其他程序创建的任务。

恢复时应先停止相关操作，在本机任务管理工具中核对 **pending 中的精确名称、旧定义和候选定义**。把事务文件作为本地证据保存，明确决定恢复旧登记还是人工移除已确认属于本次事务的登记，再整理该调度槽。不要只删除 pending 然后盲目重复安装。如果清空调度槽以重新安装，也应先人工确认并移除该槽自己的系统任务和 macOS plist；不处理任何其他名称的任务。

这是同账户的协作式安全边界。调度器重新检查文件和任务，但操作系统没有跨文件/任务的统一 compare-and-swap 事务，不能声称抵御具有同账户写权限的恶意并发替换。

## 并发、同步结果和日志

所有修改登记的操作和实际运行共享 `management.lock`；一次同步另外持有独占 `run.lock`。两者使用 O_EXCL 创建，不自动打破已有锁，不嵌套 core 的仓库锁。同步桥的 `quick-sync.lock` 统一保护手动和定时入口；调度器不重复持有同名锁。锁退出时只清除本次创建且身份与随机标记仍一致的锁，不清除替换文件。

重复运行返回 `busy` 和退出码 2，不调用同步桥、不把上一轮成功覆盖成新的成功。运行期间更改或删除登记也会忙碌失败；不会终止正在进行的用户同步命令。崩溃遗留锁需要用户确认没有活跃进程后手工处理，调度器不会根据超时或 PID 自动删除。

实际运行在当前进程内直接调用：

```python
local_bridge.run(workspace, repo, check=False)
```

它不添加 force、不跳过冲突、不改变网络失败处理，也不改写原始硬链接 skills。同步桥返回 0 或 2，调度器保留非零结果；同步异常转为失败摘要，不输出异常堆栈、原始异常字符串或 subprocess stderr。桥的上传、冲突、直接拉取等行为仍由桥负责。

日志只含时间戳、固定状态词和退出码。`latest-run.json` 最大 8 KiB；保留计数、布尔值及安全的数值字段，替换自由文本消息，不保存文件路径列表。列表或字典型统计转换为 `字段名_count`。因此这是有界、脱敏的运行摘要，不是同步桥原始响应的无损存档。错误原因需要结合用户主动查看的本地同步状态进行处理。

`runs.log` 达到 64 KiB 前轮转，最多保留当前文件及两个历史文件。`telemetry.json` 记录这些文件和 latest-run 的哈希。用户改动日志或日志写入中断导致哈希不一致时，不覆盖已有内容，下一轮不会继续同步；应先保留这些文件并人工整理运行记录。日志和登记都不保存环境变量、令牌或认证材料。

`status` 是只读操作，不创建目录或锁，返回配置周期、平台、名称、实际安装/启用/运行状态，以及最近运行摘要。无法确认的字段为 `null`；运行锁存在另行报告，不把遗留锁等同于真实进程。管理操作并发、未完成事务、系统查询错误或修改痕迹都返回退出码 2，而不是假装状态正常。

## 普通运行现实

Windows 交互用户任务要求该用户已登录；macOS 使用当前用户 GUI 会话域。机器睡眠、关机、离线或未登录时，不保证按墙上时钟精确执行，也不会主动唤醒设备。恢复可运行状态后的补跑行为由操作系统调度器决定，多个错过周期不等于逐次补齐。冲突、远端验证失败和网络错误会保留为需要检查的结果，不自动强制覆盖。

## 测试

仅需标准库，可运行：

```text
python -B -m unittest tests.test_scheduler
```

如果 tests 目录不是可导入包，可使用：

```text
python -B -m unittest discover -s tests -p test_scheduler.py
```

测试在受信任临时根目录下构造初始化仓库、工作区和假运行文件；只对临时根目录调用 resolve，以兼容 macOS 临时目录入口。命令传输和私有仓库验证均被替换，没有访问真实 home、凭据、网络或系统任务。覆盖默认/自定义周期、路径隔离、硬链接拒绝、XML/plist、安全参数、登记读回、更新删除、名称碰撞、用户改动、失败事务、同步桥非零结果、独占锁、日志轮转与错误脱敏。不修改 core 安全检查，也不通过修改全局平台伪造 Path 类型。
