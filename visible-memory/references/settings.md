# 用户配置、目录设置与恢复

程序 2.1.0；配置格式 v1；记忆库格式仍为 v2。只支持 Windows 普通本地目录，运行依赖 Python 3.11+ 标准库、SQLite FTS5 trigram。图形窗口额外需要 Tkinter；缺少 Tkinter 时仍可使用 CLI。

## 配置与读取边界

默认配置 `%LOCALAPPDATA%\VisibleMemory\config.json`，未设置环境变量则用当前用户 `AppData\Local\VisibleMemory\config.json`。`--config FILE` 覆盖该位置，恢复记录及备份随配置文件放在同级目录。

配置只包含 `schema_version=1`、正整数 `revision`、规范绝对路径 `vault_root`、库 UUID `vault_id`。不保存正文。`get` 与打开窗口只读此配置，不检查配置里的目录是否存在。记忆命令在确认本次请求后才读取配置，再核对指定库的 `_state/vault.json`；执行中目录固定，写入锁内再次核对 UUID。

缺少配置是 `NOT_CONFIGURED`，不创建文件、不打开向导。损坏配置、未知版本、路径不可用和库 UUID 不匹配均报错，不回退或静默选库。`--root` 是明确指定目录的兼容接口，不读取配置。

## CLI

```text
<Python> -B <Skill目录>\scripts\settings.py [--config FILE] get --request "查看当前记忆目录"
<Python> -B <Skill目录>\scripts\settings.py doctor --request "检查目录设置运行环境"
<Python> -B <Skill目录>\scripts\settings.py gui --request "打开记忆目录设置"
<Python> -B <Skill目录>\scripts\settings.py use --path "<已有 v2 库绝对目录>" --request "使用这个目录中的记忆库"
<Python> -B <Skill目录>\scripts\settings.py create --path "<空目录绝对路径>" --request "在这里创建空记忆库"
<Python> -B <Skill目录>\scripts\settings.py relocate --path "<目标绝对目录>" --request "把记忆库迁移到这个目录"
```

最后三个默认只读预览。确认符合已明确的要求后，用同一操作和目录，增加 `--apply --preview-token <预览返回的64位值>` 提交。token 绑定操作及前后配置、库头或文件清单，不是身份认证。预览和提交间发生变化时 `STALE_PREVIEW`，必须重新预览。不得用修改 token、锁或状态文件绕过冲突。

`use` 只检查目标路径及小型库头，不扫描正文或自动修复索引。v1 提示显式 `migrate`。`create` 只接收不存在或包含允许的空目录结构的位置；不合并非空目录。初始化后才发布配置；配置失败时新库保留并提供恢复编号。

## 窗口

窗口“可见记忆管理 — 目录设置”仅按明确请求或手动运行 `gui` 打开。首次安装、未配置和会话启动均不自动弹出。

文件夹选择只填写输入框。选择“使用已有库／创建空库／迁移当前库”后点击预览才检查必要信息；编辑目录或操作使预览失效。只有有效预览才可应用。预览和复制在线程中执行；取消在安全点停止，保留已创建的数据与恢复记录。窗口不增加正文读取权限。

## 迁移与可靠性

源目标互不包含，目标为空，且都不能与程序或配置目录冲突。拒绝磁盘根目录、网络及映射网络盘、符号链接、重解析点、硬链接和 Windows 路径别名。

迁移持有配置及源、目标库锁，记录操作意图、文件清单和匹配的锁所有者；逐文件使用临时文件复制并发布，保存修改时间；核对源未变化、目标 SHA-256、库身份、持久操作库及相对导航后，最后原子更新配置并读回。源目录永远保留。原文、历史、请求凭据、数据库和旧路径来源均不改写；不自动重建索引或导航。

持久操作库损坏、未完成库操作、活动数据库日志、未知锁或越界链接会停止迁移。已有索引、导航待刷新状态允许原样保留并报告；后续明确 `rebuild` 才刷新。

回执分别提供 `copied_files/total_files`、`data_copied`、`data_verified`、`config_applied`、`current_directory`、`phase`、`pending_recovery` 和 `record_path`。配置发布后收尾失败仍报告已切换。不要将 `ok=false` 等同于“没有保存”。

## 恢复

```text
<Python> -B <Skill目录>\scripts\settings.py recover --operation-id <UUID> --request "检查这次目录操作的恢复状态"
<Python> -B <Skill目录>\scripts\settings.py recover --operation-id <UUID> --request "继续这次目录操作" --apply --preview-token <本次恢复预览值>
```

记录保存在配置同级 `operations/<UUID>.json`，包含请求、阶段、配置前后校验、源/目标、文件清单、已复制编号和锁记录，不存额外正文。

只跳过大小、mtime 与 SHA 均符合清单的副本；部分临时文件必须逐字节等于源文件前缀才继续写入。目标未知文件、手工修改、源内容变化及配置与记录不匹配时停止。源目录、副本、恢复记录和备份均不自动清理。

仅显式 `recover --apply` 可以处理与该操作完全匹配的本工具锁；通过进程状态及创建标识证实原进程已退出后才能移除。活动、未知或无法确认进程状态的锁保留。普通命令和预览从不删锁。

库内部初始化或数据维护仍遵守原有保护；创建操作若遭遇无法安全认领的文件，保留现场并说明需要有依据的恢复，不能重新初始化覆盖。
