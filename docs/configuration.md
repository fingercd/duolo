# 注册、配置与状态

推荐从当前 Git 仓库注册：

```console
cd D:/work/my-project
wtb init --remote gpu-dev --path /home/researcher/projects/my-project --name my-project
wtb start
wtb status --short
```

`init` 只记录一对目录，不创建 Git 仓库、不提交、不覆盖文件、不立即同步。注册只需已有 Git 工作树；启动同步并建立共同基线时，两端须有初始 commit、相同的具名分支和一致的所选内容。后续命令从当前 Git 工作树发现私有配对元数据，仓库子目录也可使用；`projects` 列出已注册项目。Git worktree 的不同开发副本不能因为共享 origin 就自动视为同一配对。

`--remote` 使用现有 OpenSSH 目标或别名，`--path` 是远端绝对路径；可选 `--port` 覆盖端口，省略则保留 SSH config 设置。`--name` 设置显示名称。认证继续使用已有密钥和主机信任，注册中不保存密码或私钥。

配对配置位于当前工作树的 Git 私有目录下：`worktree-bridge/config.json`。普通仓库通常是 `.git/worktree-bridge/config.json`；linked worktree 使用它自己的 Git 私有目录，不在共享的工作文件中写配置。`init` 返回实际 `config_path`，需要显式覆盖或 MCP 启动参数时可使用该路径。

本机项目索引位于 Windows 的 `%LOCALAPPDATA%/WorktreeBridge`，或 Linux/macOS 的 `$XDG_STATE_HOME/worktree-bridge`（未设置时为 `~/.local/state/worktree-bridge`）。新注册的状态默认放在该目录的 `projects/<project_id>/state`，无需手工选择路径；迁移保留原配置中的 `state_dir`。

## JSON 配置与迁移

已有配对可迁移注册：

```console
wtb init --from-config D:/work/bridge-config/project.json
```

高级用法仍可在命令前指定 `--config PATH`，覆盖当前仓库发现的配置。配置结构：

```json
{
  "name": "my-project",
  "local_root": "D:/work/my-project",
  "remote": {
    "kind": "ssh",
    "host": "gpu-dev",
    "root": "/home/researcher/projects/my-project"
  },
  "state_dir": "D:/work/bridge-state/my-project"
}
```

| 字段 | 要求与用途 |
|---|---|
| `name` | 可选配对名称。 |
| `local_root` | 本机现有 Git 工作树绝对路径。 |
| `remote.kind` | `ssh`，或本机测试用的 `local`。 |
| `remote.root` | 另一端现有 Git 工作树绝对路径；SSH 端使用 Linux 路径。 |
| `remote.host` | SSH 目标或别名，`ssh` 模式必填。 |
| `remote.port` | 可选，省略时采用 SSH config 端口。 |
| `state_dir` | 本配对专用本机绝对路径，位于两个工作树之外。 |
| `service` | 可选后台策略，见下表。 |

本机隔离测试可使用 `"remote": {"kind": "local", "root": "D:/work/my-project-peer"}`。两端根目录独立，不可相同或相互嵌套；根目录和祖先不可含链接或 Windows reparse point。持续同步需要同一具名分支，不接管 detached HEAD 训练快照。

## 服务策略

省略 `service` 使用默认策略：

| 选项 | 默认值 | 行为 |
|---|---:|---|
| `poll_interval` | `0.25` | 增量观察间隔，秒，须大于零；不是同步延迟承诺。 |
| `reconcile_interval` | `30` | 定期强制核对间隔，秒，须大于零。 |
| `stale_after_seconds` | `10` | 观察超过此秒数后，`synced` 转成 `checking`；须为至少 1 的有限数值。 |
| `auto_sync` | `true` | 自动传输满足条件的文件变化；关闭后显式运行 `sync`。 |
| `auto_git` | `true` | 条件满足时跟进已有 commit，不自动提交每次保存。 |
| `allow_delete` | `false` | 是否允许受检查的删除传播。 |
| `port` | `0` | 本机控制端口，`0` 自动分配；这是 CLI/MCP 的内部通信地址。 |

配置更改后停止并重启服务。更换项目根或 SSH 身份使用新的状态目录，不手工编辑基线迁移身份。每个配对只运行一个控制器。

## 状态与核对

`status` 读后台缓存；未启动时提示 `start` 并失败。`status --short` 显示简短文本，`watch --interval 0.5` 持续观察变化；需要独立全量扫描时使用 `status --fresh`。

| 状态 | 含义 |
|---|---|
| `initializing` | 正在连接并检查首次状态、基线。 |
| `checking` | 旧观察已过期或需要新核对，尚不能确认一致。 |
| `synced` | 最新有效观察满足受支持文件与 Git 一致条件。 |
| `pending` / `syncing` | 有待处理差异 / 正在同步或核对。 |
| `conflict` | 内容、删除或基线条件需要选择与整理。 |
| `git_blocked` | 历史、暂存工作或活跃 Git 操作阻止跟进。 |
| `offline` | 至少一端不可达，保留观察并重新连接。 |
| `paused` | 自动同步已暂停；已开始动作可能完成。 |
| `error` | 检查或运行失败，查看 `last_error` 与事件。 |

查看 `observed_at`、`connection.*.last_seen`、`last_sync_at` 与 `last_error`。`updated_at` 表示状态发布时间，不能代替文件实际观察时间。`revision` 用于冲突选择，过期时重新查看。

`wait --timeout 30` 发起只读 `observe` 核对，要求这次核对完成且当前状态为 `synced`。它不执行文件传输或 Git 跟进，不绕过 `pause` 或 `auto_sync=false`；旧绿色缓存不会直接满足等待。写动作的 `queued: true` 只表示入队。

## 首次接入与私有记录

首次基线仅接受 HEAD、具名分支和所选文件已一致的两端。服务无法从没有共同记录的不同文件推断修改方向。先按[首次接入](onboarding.md)保全并整理，再启动；注册成功不等于已建立基线或完成同步。

私有注册、配置、状态和备份不进入项目提交。实际主机身份绑定用于防止 SSH 别名变化后误用旧配对。日志可能含项目路径与文件名，分享前审阅。中断后先查实际状态与持久记录，再恢复，不能按陈旧启动文件判断服务仍在运行。
