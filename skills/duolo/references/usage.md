# 配置与命令

控制端 Python 3.10+，远端 Python 3.9+；两端需要 Git，本机使用已有 OpenSSH 认证与主机信任。主命令为 `duo`，Python 包与模块为 `duolo`；未安装时可在既有源码/环境运行 `python -m duolo` 或 `python <source>/scripts/bridge.py`，不要重写临时同步脚本绕过检查。

公开仓库为 [fingercd/duolo](https://github.com/fingercd/duolo)，安装与可选 extra 见[项目 README](https://github.com/fingercd/duolo#readme)。`scripts/bridge.py` 是保留的源码兼容入口；新说明和日常操作使用 `duo` / `python -m duolo`。

## 注册与入口

```console
cd D:/work/my-project
duo init --remote gpu-dev --path /home/researcher/projects/my-project
duo start
duo status --short
duo watch --interval 0.5
```

`init` 只写私有注册，不执行 `git init`、提交、文件同步或覆盖。未启动时默认状态失败并提示启动，不静默改成慢扫。`--port` 可覆盖 SSH 别名端口；省略时保留配置。`--name` 设置名称，`projects` 读取本机注册列表，`init --from-config <private.json>` 迁移已有配对。

配置位于当前工作树 Git 私有目录的 `worktree-bridge/config.json`，linked worktree 各自独立；后续在仓库子目录可自动发现。高级模式保留 `duo --config <private.json> ...`。状态、备份与日志在工作树外，不发布。真实 SSH 目标、密码、token 与私钥不写入公共文档。

Duolo 0.4 保留旧配对的 `.git/worktree-bridge`、Windows `%LOCALAPPDATA%/WorktreeBridge`、Linux/macOS `worktree-bridge` 状态布局及 `X-WTB-*` 请求头，使原配置和记录继续可用。它们是内部兼容名称，不需要改成 `duolo`；实际位置以 `init` 返回的配置路径和已有 `state_dir` 为准。

两端要有初始 commit、同一具名分支；第一次基线只接受 HEAD 与所选文件相同。已有分叉先盘点、保全和处理，不能由 mtime、主机角色或 origin 自动判断权威端。CRLF/LF 按真实字节比较，先审阅 `.gitattributes`，不暗中全仓转换。

## 状态与同步

```console
duo status
duo status --fresh
duo sync --wait --timeout 30
duo wait --timeout 30
duo pause
duo resume
```

`status` 默认缓存 JSON，`--short` 简短文本，`watch` 持续显示变化与事件。缓存响应不证明观察新鲜：核对 `observed_at`、连接、`checking`、`last_error`、HEAD/分支和冲突。`status --fresh` 是独立全量扫描，不启动服务。

`sync` 请求受检查的写入，`queued: true` 只表示接受。`wait` 发起只读新观察，等待该观察完成且状态为 `synced`，不执行传输或 Git 跟进，不绕过 `pause` / `auto_sync=false`。从保存前留下的绿色缓存不能直接满足等待。

```console
duo conflicts
duo resolve src/model.py --take remote --revision TOKEN
duo wait --timeout 30
```

冲突选择会覆盖另一端，必须审阅内容并引用刚返回的 revision。版本过期时重读状态，不能把旧选择当对后续变化的授权。删除默认关闭，配置允许时仍执行基线与内容核验。

## Git 协调

后台自动跟进任一端已有 commit，要求同分支、从记录基线真正前进且目标改动可保全。历史分叉、独立暂存工作、活跃 Git 操作或保护条件不满足时返回 `git_blocked`。不会每次保存自动 commit、复制 `.git`、自动 merge/rebase/stash/reset、强推或向项目 GitHub 推送。

新增历史在传输前审核，包括最终树已删除的中间文件；上限为 128 个提交、4096 个逐提交路径条目、8 MiB bundle，并审核排除路径、文件大小与模式。超限需要经审阅的 Git 原生流程，不放宽工具检查。

```console
duo checkpoint -m "experiment setup" --tag exp-001
```

显式 checkpoint 两端先一致后从本地创建所选工作的 commit/tag，并让远端跟进。它不调用 `git commit`、不运行 commit hooks；依赖 hooks 的检查先单独执行，或按项目流程手动提交。Skill 本身不授予提交、发布或训练权限。

手动处理分叉时暂停自动同步并记录两端状态，按当前授权保全各自工作，再采用项目要求的原生 Git 操作。不能默默丢弃 staged/dirty 工作。双方新 Git 状态核对后才更新共同基线，不沿用旧 HEAD 的内容基线解释新改动。

## 手动计划兼容

```console
duo stop
duo baseline
duo plan --out D:/work/bridge-state/my-project/plan.json
duo apply --plan D:/work/bridge-state/my-project/plan.json
```

这些命令保留原显式工作流；运行中的服务阻止旧 `apply` / `baseline` 并行写状态。计划路径放在工作树之外，不能覆盖配置或基线。旧路径仍拒绝删除、冲突和过期计划；`baseline --refresh` 只能记录双方已一致的状态，不用于绕过分叉。

## MCP

可选 `mcp>=1.28,<2` 官方 SDK extra 提供固定项目 stdio 工具，与 CLI 共用服务。先在既有环境安装源码 extra，再在已注册仓库运行 `duo mcp`；客户端不能设置工作目录时使用注册返回的 `config_path` 明确绑定。工具包含状态、同步、暂停/恢复、冲突列表/选择、等待、checkpoint，无任意 shell 或替换目录接口。

## 失败恢复

连接中断或动作失败时可能部分完成。先读当前状态、最近动作和状态目录中的持久记录、目标旧内容备份，必要时独立全量核对；不盲目重放批次，不自动回滚仍在变化的文件，不手改基线。Git ref 已前进但 index 尚未发布的失败可能保留锁与阶段记录，需按真实状态审阅后恢复。

## 交接

记录明确配对、两端 HEAD/分支、观察时间、同步/动作结果、未处理冲突、实际重新读取的规则。实验另记冻结 SHA/tag、配置、seed、数据/权重与产物摘要；不把全量日志、机器会话存储或凭据塞入公共 `AGENTS.md`。
