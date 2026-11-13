# 每台设备生成快捷入口

快捷入口在当前电脑运行 AgentSync。程序代码可以公开；规则、技能、人设、记忆以及本机路径放在源码目录之外，资料通过你自己的 GitHub 私有仓库交换。

## 三个独立目录

先按[新手上手指南](getting-started.md)完成内容仓库与本机文件绑定。准备三个互不嵌套的目录：

- **程序目录**：克隆的 `agent-sync` 源码。
- **资料目录 CONTENT**：克隆的 GitHub 私有资料仓库，包含 manifest 与本机 `.agent-sync` 状态。
- **本机工作区 LOCAL**：保存技能选择清单、普通文件镜像、快速核对状态和生成的入口，仅留在当前电脑。

另一台电脑要重新绑定自己的路径并生成入口，不能复制第一台电脑的 `.agent-sync` 或直接沿用绝对路径。

## 生成入口

在程序目录执行，将 `CONTENT`、`LOCAL` 替换为本机绝对路径：

```sh
python -m agent_sync --repo "CONTENT" shortcut --workspace "LOCAL"
```

Mac 使用 `python3`。程序自动选择当前系统，也可用 `--platform windows` 或 `--platform macos` 明确目标。默认入口放入本机工作区的 `launchers` 目录；用 `--output "绝对输出目录"` 可以指定桌面等位置。

输出目录不得占用工作区的 `selection.json`、`collection-state.json`、`fast-sync-state.json`、`quick-sync.lock`、`sources.json`、`sources-state.json`、`skill-mirror` 或 `schedule`，也不能是这些位置的祖先或子路径。默认 `LOCAL/launchers` 与外部桌面目录均可使用。

Windows 入口使用本机 Python 和 Git，提供可双击的启动文件及快捷方式；Mac 入口是可执行的 `.command` 文件。生成器不覆盖内容不同的现有文件，重新运行时会先检查目标。更新 Python、Git 或程序目录后应重新核对生成入口。

入口仅将生成时确认的 Git 绝对目录放在子进程 `PATH` 首位，随后保留当前用户 `PATH` 中绝对路径项的原有顺序。空项、当前目录和其他相对目录被过滤，不补入平台默认搜索目录，也不修改系统 `PATH`。运行结果使用固定中文提示区分远端更新、本机待推提交、技能安装审阅和已暂存改动。

入口引用当前机器路径，不嵌入规则正文、聊天或访问令牌。不要把工作区与入口提交到公开源码仓库。

## 快捷同步做什么

也可以直接运行：

```sh
python -m agent_sync --repo "CONTENT" quick-sync --workspace "LOCAL"
python -m agent_sync --repo "CONTENT" quick-sync --workspace "LOCAL" --check
```

第一次运行完成完整核对；之后没有变化时使用快速路径，仍读取实际文件并检查远端。相同硬链接的技能原件在单次扫描中只读取一次。有本机变化时，执行收集、冲突检查和正常上传；`--check` 只核对文件，不刷新对话来源、不写入或上传。手动与定时入口共用工作区独占锁；已有同步正在运行时，本次会停止。

远端出现新提交、已有本地待推提交、冲突或技能镜像独立修改时，入口停止并给出需处理的状态。请让助手查看差异并完成收取或合并。快捷入口不会擅自覆盖原始技能文件。

## 技能选择清单

直接绑定的规则、人设图片和 Markdown 记忆可以使用已有资料绑定。技能通过本机工作区的 `selection.json` 显式选择；生成器在缺少清单时只创建空模板，不扫描主目录，也不会宣称技能已接入。

```json
{
  "version": 1,
  "files": [
    {
      "source": "/absolute/local/skill/SKILL.md",
      "mirror": "shared/example/SKILL.md",
      "reviewed_sha256": "替换为审阅过的文件SHA256"
    }
  ]
}
```

`source` 指当前设备的真实文件路径；`mirror` 位于工作区 `skill-mirror` 下，以 `codex`、`shared` 或 `antigravity` 开头。将相应技能同步组绑定到该普通镜像目录。原始技能可以是经过检查的普通硬链接，镜像必须是独立普通文件；junction 或符号链接必须由用户先确认真实来源，程序不自动跟随。

清单、收集基线和快速核对状态仅属于当前设备。新增技能文件需要审阅后加入清单。远端技能更新进入镜像后，需要核对并安装到助手实际技能目录，不能把旧原件直接盖回新镜像。

## 私有资料校验

生成入口、快捷同步和 `git-sync` 都核对资料仓库。网络目标必须是 `github.com` 上可确认的私有仓库；公开仓库、无法读取私有属性、不同的获取与上传目标均会停止。程序不会自动修改仓库可见性。

认证优先使用 `GH_TOKEN` 或 `GITHUB_TOKEN` 环境变量，其次使用系统 Git 凭证管理器已有的 GitHub HTTPS 凭证。令牌只在内存中发送给固定的 GitHub 官方 API，不写入入口或配置文件，不跟随 API 重定向。SSH 可用于 Git 传输，但私有属性核验仍需 API 可用凭证。

本地路径和 `file://` Git 仓库保留用于离线工作与测试；它们没有 GitHub 可见性属性。其他网络 Git 托管服务当前不支持这个私有属性校验。

## 自动归档选定对话

在 `LOCAL/sources.json` 登记需要随同步刷新的来源。文件不存在时只同步已绑定资料；空 `sources` 数组表示没有选定对话。程序不扫描主目录或会话列表。

以下示例中的路径和会话 ID 都需换为本机真实值。Windows JSON 路径中的反斜杠要写成 `\\`，也可以使用正斜杠：

```json
{
  "version": 1,
  "sources": [
    {
      "id": "project-codex",
      "kind": "codex",
      "thread_id": "SOURCE_THREAD_ID",
      "command_file": "C:/Users/Alice/AgentSyncExports/codex-argv.json",
      "timeout_seconds": 60
    },
    {
      "id": "project-dsh",
      "kind": "file",
      "path": "C:/Users/Alice/AgentSyncExports/dsh-transcript.json",
      "format": "transcript-v1"
    }
  ]
}
```

`codex-argv.json` 是 UTF-8 JSON 参数数组，例如 `["C:/Tools/Codex/codex.exe", "app-server", "--stdio"]`；Mac 换为当前安装的实际入口。仅使用自己信任且支持这些公开接口的命令。导出文件和命令配置必须是普通文件，位于程序、私有资料库、本机工作区这三个目录之外，不能使用链接。实际清单、会话标识和本机路径不写入公开源码。

| 来源 | 必填字段 | 可选字段与边界 |
| --- | --- | --- |
| `codex` | `id/kind/thread_id/command_file` | `timeout_seconds` 默认 30、范围大于 0 且不超过 300；`max_pages` 默认 1000、范围 1..1000；`project_id` 仅可省略或为 null |
| `file` | `id/kind/path/format` | `format` 为 `text`、`agy-result` 或 `transcript-v1`；`text` 必须提供 `tool`，可提供 `source_id/project_id`；JSON 格式的这些可选字段只能核对已有元数据，不能覆盖它们 |

`id` 使用知识组的安全命名规则，清单内不得重复，最多 100 个来源。顶层可设置 `limit_bytes`，默认和最大值均为 16777216。输入格式和归档完整度见[历史详解](history.md)；`agy-result` 的 `project_id` 也只能省略或为 null。

正常快捷同步会刷新选定来源。Codex 读取指定线程的公开分页历史；文件来源读取你已导出的内容，不主动操作 AGY/DSH 内部数据库。原始内容有变化才建立新档案，旧档案始终保留；Codex 的捕获时间不计为内容变化。`--check` 不刷新来源、不创建档案。来源失败会停止本轮同步，并保留已经产生的不可变档案供核对。

任务、交接和项目登记随私有资料库上传。项目代码使用独立的 `project sync PROJECT_ID` 命令；快捷入口与定时任务不会替你提交或同步项目源码工作树。

需要每小时自动运行时，见[定时同步](scheduling.md)。
