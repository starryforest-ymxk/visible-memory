# visible-memory 2.1.0 安装与使用

这是一个可直接分发的 Windows Codex Skill：Markdown 是正式原文，SQLite 提供本地关键词检索，分页 Markdown 导航供人阅读。每位系统用户配置一个当前记忆目录，可以按需切换或迁移。

**只有本次明确请求才检索、写入或设置目录。** 普通任务不读用户配置或记忆库。没有后台监听、自动召回、首次设置向导、插件、独立 EXE 或云同步。Codex 原生记忆不自动导入、同步或修改。

## 环境

Python 3.11+，标准库 SQLite 必须支持 FTS5 trigram。目录窗口需要 Tkinter。请用你本机可用的 Python 完整路径；Codex 桌面可通过 `load_workspace_dependencies` 找到现有运行时。不需要安装第三方运行依赖。

以下 PowerShell 示例先自行填写变量：

```powershell
$vmPython = '<可用 Python 完整路径>'
$vmSource = '<解压后的 visible-memory 文件夹绝对路径>'
& $vmPython -B "$vmSource\scripts\settings.py" doctor --request '检查运行环境'
```

## 新安装

从源码 ZIP 解压，校验下载附带的 SHA-256。源码内 `release.json` 记录每个发布文件的校验值。

```powershell
& $vmPython -B "$vmSource\scripts\install.py" --request '安装可见记忆管理 Skill'
& $vmPython -B "$vmSource\scripts\install.py" --request '安装可见记忆管理 Skill' --apply
```

默认安装当前用户 `.agents\skills\visible-memory`，默认全局引导为 `CODEX_HOME` 下的 `AGENTS.md`，未设置 `CODEX_HOME` 则为 `.codex\AGENTS.md`。安装只处理程序与带标记引导，不创建个人配置或记忆库，也不打开设置窗口。

如果已有安装，使用下一节的明确目标路径，避免同名安装两份。安装后新会话加载 Skill；正在进行的会话可能仍保留旧引导，普通任务不会因此读取记忆。

## 代码升级

```powershell
$vmInstalled = '<现有 Skill 安装目录绝对路径>'
& $vmPython -B "$vmSource\scripts\install.py" --target $vmInstalled --upgrade-code --request '升级现有 Skill 代码到 2.1'
& $vmPython -B "$vmSource\scripts\install.py" --target $vmInstalled --upgrade-code --request '升级现有 Skill 代码到 2.1' --apply
```

可用 `--agents-file`、`--backup-dir` 明确指定全局引导及备份位置。可携带预览返回的 `--preview-token` 核对状态变化。只接受完整已知版本或校验清单匹配的已管理版本；用户修改或未知文件先报告具体路径及校验差异，不强行覆盖。

修改前保存旧 Skill、全局引导和校验清单。托管块外的字节保留，重复安装不重复引导。失败时恢复本次代码和引导；若手工修改使恢复不安全，保留备份并报告冲突。中断后 `install.py --recover-code <操作UUID> --backup-dir <备份目录> --request '恢复中断的代码升级'` 默认预览，加 `--apply` 才恢复。代码恢复不改用户配置或记忆数据。

旧 `--upgrade` 与 `--rollback` 保留为明确的 v1→v2 **库格式**迁移及受限回退入口，不能代替 `--upgrade-code`。迁移后有新写入时不得恢复旧库快照。见 [操作说明](references/operations.md)。

## 主动设置目录

在 Codex 中说“打开记忆目录设置”，或者手动运行：

```powershell
# 默认安装位置；自定义安装时请改为实际路径。
$vmInstalled = Join-Path $env:USERPROFILE '.agents\skills\visible-memory'
& $vmPython -B "$vmInstalled\scripts\settings.py" gui --request '打开记忆目录设置'
```

选择新目录及操作，先预览、再应用。已有库使用“使用已有库”；新的位置用“创建空库”；希望连同内容移动则用“迁移当前库”。迁移是复制并核验后切换配置，**原目录保留**。

也可以明确请求“查看当前记忆目录”“使用这个目录中的记忆库”“把记忆库迁移到这个目录”。CLI 的预览、提交与恢复见 [目录设置](references/settings.md)。窗口打不开时这些 CLI 命令仍可使用。

配置保存在 `%LOCALAPPDATA%\VisibleMemory\config.json`；同级有 `operations/` 和 `backups/`。没有环境变量时使用当前用户 `AppData\Local\VisibleMemory`。只读操作不会创建任何目录或文件。配置损坏或库身份不匹配时明确报错，不静默选另一套库。

## 使用记忆

说“记住：……”保存指定内容；“检索关于……的记忆”按关键词检索；“读取这条记忆”读取指定 UUID；“忘记这条记忆”归档；“恢复这条记忆”恢复。普通聊天不会自动保存。

```powershell
& $vmPython -B "$vmInstalled\scripts\memory.py" search --request '检索写作风格的记忆' --query '写作风格'
```

默认最多 8 个摘要，再按需读取命中原文。中文单字、双字和长词可检索。历史和归档仅在明确要求时纳入。手改正文下一次明确检索即时参与结果；总览和索引只有明确维护才持久刷新。

仍支持旧 `memory.py --root <DIR>` 和编程接口 `Vault(root).execute()`。明确指定配置用 `--config FILE`；二者互斥。详见 [格式](references/format.md)、[操作说明](references/operations.md)。

## 分发、验证和停用

运行 `scripts/build_release.py --output <输出目录>` 可生成带版本的源码 ZIP 及 SHA-256 文件。只包含 Skill、程序、参考说明、发布清单和虚构测试，不包含配置、记忆、数据库、历史、任务报告或 Python 缓存。不会外部发布。

隔离验证：`<Python> -B -m unittest discover -s tests -v`。规模验证用 `tests/benchmark_v2.py --workspace <隔离目录> --output <报告JSON> --runs 30`，只创建虚构数据；切勿指向个人库。GUI 绑定测试与实际窗口、文件夹选择人工验收分开记录，不用导入 Tkinter 代替窗口验收。

停用时删除 `AGENTS.md` 的 `visible-memory:start/end` 托管块并移除 Skill 文件夹。**保留个人配置和记忆数据**；本工具没有自动清理、永久擦除或卸载数据命令。现有 Markdown 仍可直接阅读。

## 许可证

本项目使用 MIT License，见 [许可文本](references/license.md)。
