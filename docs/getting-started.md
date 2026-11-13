# 新手上手指南：从第一台电脑同步到第二台电脑

本指南先用**一个试用规则文件**跑通两台电脑的同步，再接入正式规则、技能、人设和长期记忆。试用文件只用于验证复制流程，AI 助手不会自动读取它。

返回主页：[README](../README.md) · 进阶查阅：[命令与技术参考](reference.md) · [历史归档与能力边界](history.md) · [验证记录](verification.md)

---

## 开始前的三条基本约定

先安装 Python 3.11+ 和 Git，确认终端能运行 `python --version`（Mac 通常用 `python3 --version`）和 `git --version`。工具源码仓库目前也是私有仓库，克隆时需要相应的 GitHub 访问权限。

1. **运行同步命令前，进入 `agent-sync` 工具源码目录**
   本指南采用直接运行源码的方式，无需把 AgentSync 安装到系统全局环境。每次执行 `python -m agent_sync` 前，先进入克隆下来的工具源码目录。也可以把本页步骤交给 AI 助手，让它在终端代为执行。
2. **注意本机的 Python 命令名称**
   下文通用示例写作 `python`。macOS 系统通常默认只有 `python3`（Windows 也可使用 `py -3.11`，或使用你自行配置的命令），请不要假设每台电脑都自带 `python` 别名，按本机实际命令替换即可。另外，`--repo "资料库路径"` 必须写在 `init`、`plan` 等子命令**前面**。
3. **工具源码与私人资料目录必须完全分开**
   - **工具源码目录**：存放 `agent-sync` 程序本身。
   - **私有资料目录（下文简称 `CONTENT`）**：存放你要同步的个人资料。可以放在工具目录旁的同级文件夹，或用户主目录下独立的 `AgentSyncData/my-agent-data` 文件夹；填写完整路径，**不能放在 `agent-sync` 目录内部或与之重叠**。
   - 更新工具程序（在 `agent-sync` 目录 `git pull`）与同步个人资料（针对 `my-agent-data` 运行同步命令）是互不干扰的两件事。

---

## 第一步：在 GitHub 创建空白的私有资料仓库

1. 登录 GitHub（或其他兼容的标准 Git 托管平台），新建一个名为 `my-agent-data` 的仓库。
2. 将可见性设为 **Private（私有）**。
3. 本指南按**空仓库首次推送**操作：创建时不添加 `README`、`.gitignore` 或 `LICENSE`。AgentSync 会在本机生成所需的保护与跨平台换行规则文件。
4. 准备好本机 Git 访问该私有仓库的权限（推荐使用 SSH 密钥，或系统自带的 Git 凭证管理器）。**切勿使用把密码或个人 Token 直接写进网址的远端 URL。**

---

## 第二步：配置第一台电脑（以公司 Windows 为例）

我们先新建一个独立的试用 Markdown 文件，随后把它登记为要同步的文件。正式接入时同样选择具体规则文件或技能子目录，整个 `.codex`、`.dsh`、`.gemini` 目录不属于同步范围。

### 1. 明确你要替换的两个路径占位符

请将下文命令中的引号内路径替换为你自己电脑上的真实直接路径（路径中不要包含 `..`，也不要使用快捷方式或符号链接）：

| 占位符 | 含义 | Windows 实际路径示例 | macOS 实际路径对照示例 |
| --- | --- | --- | --- |
| `CONTENT` | 本机私有资料仓库目录 | `"C:\Users\Alice\AgentSyncData\my-agent-data"`<br>（或工具目录旁 `"C:\Users\Alice\Tools\my-agent-data"`） | `"/Users/alice/AgentSyncData/my-agent-data"`<br>（或工具目录旁 `"/Users/alice/Tools/my-agent-data"`） |
| `LOCAL_RULE` | 本机已存在的单个试用规则文件 | `"C:\Users\Alice\AgentSyncTrial\trial-rule.md"` | `"/Users/alice/AgentSyncTrial/trial-rule.md"` |

> 先在本机创建好 `trial-rule.md`（例如在 `C:\Users\Alice\AgentSyncTrial\trial-rule.md` 中随便写入一行 `# 我的跨设备试用规则` 并保存），确认该文件已存在后再继续。

### 2. 克隆工具源码并进入目录

打开终端，把 `agent-sync` 克隆到独立位置并进入该目录：

```sh
git clone https://github.com/FacelessAkari/agent-sync.git "C:\Users\Alice\Tools\agent-sync"
cd "C:\Users\Alice\Tools\agent-sync"
python -m agent_sync --help
```

（如果第一台电脑是 Mac，将目录换成如 `"/Users/alice/Tools/agent-sync"`，并将 `python` 换成 `python3`。）

### 3. 初始化资料库、登记共享组并收集文件

保持在工具源码目录中，把下面的 Alice 示例路径换成自己的完整路径，再依次执行这 5 条命令：

```sh
python -m agent_sync --repo "C:\Users\Alice\AgentSyncData\my-agent-data" init
python -m agent_sync --repo "C:\Users\Alice\AgentSyncData\my-agent-data" add trial-rules --category rules --kind file --path data/rules/trial-rule.md
python -m agent_sync --repo "C:\Users\Alice\AgentSyncData\my-agent-data" bind trial-rules "C:\Users\Alice\AgentSyncTrial\trial-rule.md"
python -m agent_sync --repo "C:\Users\Alice\AgentSyncData\my-agent-data" plan
python -m agent_sync --repo "C:\Users\Alice\AgentSyncData\my-agent-data" apply --direction push
```

这几步分别做了什么：

- `init`：在本地创建资料仓库结构与保护文件（不会联网）。
- `add`：在共享清单里登记一个名为 `trial-rules` 的单文件同步组，存放在资料库内部的 `data/rules/trial-rule.md`。
- `bind`：告诉这台电脑，`trial-rules` 对应本机的 `trial-rule.md`。
- `plan`：检查差异，此时应看到 `trial-rules` 状态为 `push`。
- `apply --direction push`：把本机的 `trial-rule.md` 复制进本地资料库的 `data/rules/trial-rule.md`（**注意：这一步只是本机文件复制，还没有上传到 GitHub**）。

### 4. 初始化资料库 Git 并首次推送到私有远端

把下面命令中的 `"CONTENT"` 替换为你的本地资料库路径（如 `"C:\Users\Alice\AgentSyncData\my-agent-data"`），并将远端地址替换为你第一步新建的空白私有仓库地址：

```sh
git -C "CONTENT" init -b main

# 如果尚未配置 Git 提交身份，用自己的姓名和邮箱填写下面两行；这里仅设置当前资料仓库：
git -C "CONTENT" config user.name "Your Name"
git -C "CONTENT" config user.email "you@example.com"

git -C "CONTENT" add -- manifest.json .gitignore .gitattributes data/
git -C "CONTENT" commit -m "Initialize agent sync data"
git -C "CONTENT" remote add origin git@github.com:你的用户名/my-agent-data.git
git -C "CONTENT" push -u origin main
```

> **重要**：**必须原样保留 `init` 生成的 `.gitignore` 和 `.gitattributes`，并在首次推送时一并提交。** 它们负责隔离每台电脑私有的本机状态目录，并保证 Windows 与 macOS 之间同步时不因换行符转换而损坏文件，请勿删除或修改。

推送成功后，第一台电脑的试用规则就保存在你的私有仓库里了。

---

## 第三步：配置第二台电脑（以家里 Mac 为例）

在第二台电脑（Mac 或另一台 Windows）上，先安装好 Python 3.11+ 和 Git，并配好访问同一个 `my-agent-data` 私有仓库的权限。

这台电脑如果尚未配置 Git 提交姓名和邮箱，在下面克隆资料仓库后，也按第一台电脑的 `git -C "CONTENT" config user.name / user.email` 示例设置。两台电脑各自需要提交身份和访问权限。

### 1. 单独克隆工具源码并进入目录

```sh
git clone https://github.com/FacelessAkari/agent-sync.git "/Users/alice/Tools/agent-sync"
cd "/Users/alice/Tools/agent-sync"
```

### 2. 先克隆已有的私有资料仓库到独立路径

把第一台电脑推上去的 `my-agent-data` 克隆到本机独立位置，与 `agent-sync` 工具源码目录分开：

```sh
git clone git@github.com:你的用户名/my-agent-data.git "/Users/alice/AgentSyncData/my-agent-data"
```

### 3. 初始化本机状态、绑定本机路径并拉取文件

先确保这台电脑上准备存放试用文件的父文件夹已经建好（例如先创建空文件夹 `"/Users/alice/AgentSyncTrial"`），然后保持在 `agent-sync` 工具源码目录中依次执行：

```sh
python3 -m agent_sync --repo "/Users/alice/AgentSyncData/my-agent-data" init
python3 -m agent_sync --repo "/Users/alice/AgentSyncData/my-agent-data" bind trial-rules "/Users/alice/AgentSyncTrial/trial-rule.md"
python3 -m agent_sync --repo "/Users/alice/AgentSyncData/my-agent-data" plan
python3 -m agent_sync --repo "/Users/alice/AgentSyncData/my-agent-data" apply --direction pull
```

请留意第二台电脑的三个关键区别：

- **这里的 `init` 只建立本机私有状态**（生成不进 Git 的本机记录目录），不会清空克隆下来的共享资料；切勿从第一台电脑手动拷贝 `.agent-sync` 隐藏目录。
- **第二台电脑不需要再运行 `add`**：共享组 `trial-rules` 已经写在克隆下来的 `manifest.json` 里了，直接 `bind` 同一个组名即可，且本机路径完全可以和第一台电脑不同。
- **如果本机目标位置原本已有内容不同的同名文件**：首次运行 `plan` 会提示 `conflict`（冲突），`apply` 会整批停止写入以保护你原有的文件。此时**不要尝试强行覆盖或直接删除**，请先备份本机原有文件，人工比对两边内容并确认合并方案——例如先把旧文件移到备份目录再执行 `apply --direction pull`，或把人工合并后的最终结果同时保存到本机目标路径和资料库对应文件后再重新运行 `plan` 与 `apply`。

现在打开 `"/Users/alice/AgentSyncTrial/trial-rule.md"`，就能看到第一台电脑写下的内容已经出现在第二台电脑上了。

---

## 第四步：日常收发节奏与冲突处理

两台电脑都绑定完成后，日常不需要再跑 `init`、`add` 或 `bind`，只需在 `agent-sync` 工具源码目录中按下面的固定顺序收发：

### 日常收取（开始工作前）

当本机资料库**没有尚未处理的改动**时，按以下顺序收取另一台电脑的更新：

```sh
python -m agent_sync --repo "CONTENT" git-sync
python -m agent_sync --repo "CONTENT" plan
python -m agent_sync --repo "CONTENT" apply --direction pull
```

- **注意 `git-sync` 是双向同步**：它不仅会从远端拉取更新，如果发现你本地资料库（`manifest.json` 或 `data/`）里已经有尚未上传的改动，它也会一并校验、提交并推送到远端；同时它**不会**自动把文件写进你的 AI 工具目录，必须再看 `plan` 并执行 `apply --direction pull` 才会落到本机绑定路径。

### 日常发送（结束工作后）

在本机修改完绑定的文件后，按以下顺序收集并上传：

```sh
python -m agent_sync --repo "CONTENT" plan
python -m agent_sync --repo "CONTENT" apply --direction push
python -m agent_sync --repo "CONTENT" git-sync
```

- **如果 `git-sync` 顺带合入了别机的新内容**：假如另一台电脑刚才更新了其他同步组，`git-sync` 会把那些远端改动合并进你的本地资料库。此时建议再跑一次 `plan` 和 `apply --direction pull`，让本机文件也保持最新。

### 两台电脑同时改了同一份文件怎么办？

先保留两边的内容，再按提示处理：

- 如果两台电脑在未同步期间各自修改了同一份文件，可能在 `git-sync` 合并阶段报错中止，或在 `plan` 时显示 `conflict`。
- 如果 `plan` 显示 `conflict`，整批 `apply` 不会改写文件。比对合并后，将确认的结果分别保存到**本机绑定文件和本地资料库对应文件**，再运行 `plan` 与 `apply` 建立共同版本。
- 如果 `git-sync` 报 Git 合并冲突，先完成资料仓库的 Git 合并，再继续同步。可以把报错交给 AI 助手，要求它保留两侧提交、展示差异并协助合并；只改本机绑定文件不能解决 Git 分支冲突。

---

## 第五步：接入真实 AI 助手与后续扩展

当你用 `trial-rule.md` 熟悉了收发手感后，就可以把正式配置接入进来：

1. **确认 AI 助手真正读到了文件（不要假设自动热加载）**
   文件复制到本机路径后，不能保证所有 AI 工具都会实时热加载。每次更新规则或技能后，请重启工具或新建会话，并核对工具当前版本实际读取的配置文件路径。
2. **永远按需绑定具体文件或子目录**
   只绑定明确的规则文件（如具体规则入口 `AGENTS.md`）或专门的技能文件夹，**切勿直接绑定或拷贝整个 `.codex`、`.dsh` 或 `.gemini` 主目录**。
3. **逐步扩展更多能力**
   - 同步整个技能文件夹（`--kind tree`）、为不同助手保留各自独立的人设、建立公共 Markdown 长期记忆目录（如 `~/AgentSyncKnowledge`），以及登记业务项目 Git 同步、任务与工作交接：请查看 [命令与技术参考](reference.md)。
   - 归档公开可见的聊天记录、导出 Markdown 会话交接单或向新 Codex 会话恢复受支持的文本上下文：请查看 [历史归档与能力边界](history.md)。
