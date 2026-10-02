# Worktree Bridge

给本地 Agent 与 SSH 服务器使用的 Git 工作树协同原型：先看清两端的代码、文档与 Git 状态，再生成可审阅的文件同步计划。同一个文件在两端发生不同修改时，停止并报告。

**状态：实验原型。** 当前提供按命令触发的 CLI 与 Agent Skill；没有后台监听、自动 Git 历史同步或 MCP 服务。需要两端停止编辑再执行写入，不能承诺任意并发编辑下零丢失。已有项目第一次接入时，需要先保全并处理历史分叉。

## 适用场景

Agent 在 Windows 本地读取和修改项目，训练、评测或部分代码修改在 Linux 服务器进行。希望项目源码、`AGENTS.md`、`CONTEXT.md` 等说明有可检查的共同状态，同时不把训练数据、权重和 Git 内部文件复制到另一端。

本工具比较文件内容和 Git 状态，但不代替 Git 管理提交。仅复制文件不会让另一端自动出现相同的 commit；直接复制 `.git` 也不可取。原型在 HEAD 或分支不一致时停止，要求先按项目约定协调 Git 历史。

如果主要需要成熟的持续文件同步，请先评估 [Mutagen](https://mutagen.io/documentation/synchronization/) 或 [Unison](https://github.com/bcpierce00/unison)。本项目实验的是面向 Agent 的配对、状态检查和计划执行边界，不声称比这些工具更成熟。

## 安装和配置

本地需要 Python 3.10+、Git 与 OpenSSH 客户端；SSH 目标需要 Python 3.9+ 与 Git。使用已配置好的 SSH 密钥和主机信任，不在配置文件里保存密码或私钥。

从源码目录安装到项目自己的 Python 环境：

```console
python -m pip install .
```

开发时也可以不安装，在源码目录直接使用小型入口（它调用同一套核心代码）：

```console
python scripts/bridge.py --help
python scripts/bridge.py --config project.local.json status
```

或者在 PowerShell 中将当前源码目录的 `src` 加入本次终端的 `PYTHONPATH`，再运行模块。

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m worktree_bridge --help
```

复制 [示例配置](examples/bridge.example.json) 为仓库外的 `project.local.json`，填写明确的一对工作目录。`host` 可以是现有 SSH config 的别名。`state_dir` 应是该配对专用、本机私有、位于两个项目目录之外的目录，保存基线、执行记录和备份。不要把它或真实服务器配置放进公开仓库。

测试可把 `remote` 换为 `{"kind":"local","root":"/path/to/second-checkout"}`，从而在本机使用两个独立 Git checkout。

## 工作流

```console
python -m worktree_bridge --config project.local.json status
python -m worktree_bridge --config project.local.json baseline
python -m worktree_bridge --config project.local.json plan --out D:/worktree-bridge-state/my-project/plan.json
python -m worktree_bridge --config project.local.json apply --plan D:/worktree-bridge-state/my-project/plan.json
```

1. `status` 读取两端 Git 状态和受支持文件的内容指纹，不修改项目文件。
2. `baseline` 记录已经相同的两端作为共同基线。首次内容不一致、HEAD 不一致或分支不一致时拒绝建立基线，不猜测哪边是新的。
3. 之后两端都可以编辑；`plan` 根据“上次共同状态、本地现在、远端现在”判断传输方向。计划写到本地指定文件。
4. 审阅计划并暂停编辑者后，显式 `apply`。同一文件两端改成不同内容、删除、版本分叉或过期计划会阻止执行。
5. 执行后再读 `status`，确认结果和关键文档。修改了 Agent 规则后，让 Agent 重新读取；文件到达另一台机器不等于已运行 Agent 的上下文自动刷新。

两端 Git 提交或分支相对于基线发生变化时，原型停止。先通过 Git 原生机制和人工审阅协调两端；确认两端 Git 和所选文件都相同后，使用 `baseline --refresh` 显式刷新，旧基线会保留。它不能用来跳过已有文件冲突。

所有命令输出 JSON，便于 Agent 和后续 MCP 适配器调用。失败使用非零退出码；不要把“SSH 命令发出”当成“同步完成”。

上面的计划文件示例放在项目外的私有状态目录。不要在任何一个被同步的工作树里生成计划文件，否则计划本身会成为新的文件改动；CLI 会拒绝这类本地输出路径。

## 当前边界

- 只同步 Git 已跟踪文件和非忽略的未跟踪普通小文件；额外的内置排除规则会进一步缩小范围。查看返回的跳过/问题信息，不把受支持文件的相同声称为整个目录相同。
- 当前单文件上限为 1 MiB。`data/`、`datasets/` 中已被 Git 跟踪的受支持源码可参与同步，例如 `datasets/build.py`；其中未跟踪文件仍排除。
- Git 元数据、常见密钥和 `.env`、常见数据/产物目录与模型权重、符号链接和嵌套仓库不作为普通文件传输。`models/` 可能包含模型源码，不能一概当作权重目录排除。名称过滤不等于完整的秘密检测，用户仍需正确的 `.gitignore` 和公开发布审查。
- 保留字节内容，不隐式转换 CRLF/LF。Windows/Linux 的换行差异也会阻止首次基线；建议在 Git 中约定 `.gitattributes`，由用户审阅后统一，不能偷偷全仓重写。
- 不自动传播删除、不自动解决冲突、不自动 merge/rebase/stash/commit/push、不复制 `.git`，也不镜像 Git 暂存区。
- 需要可解析的初始提交和具名分支；不接管无提交的新仓库或 detached HEAD 运行快照。基线刷新不能丢弃旧基线文件，因此涉及删除的 Git 历史更新仍需单独整理接入。
- 只传文件内容，不同步 Unix 执行位、所有者或 ACL。覆盖现有普通文件时沿用目标权限，新增文件使用临时文件的默认权限；远端执行新脚本前需另行检查权限。
- 逐文件原子替换不是跨文件、跨主机事务。失败可能已有部分文件成功；需根据日志和备份恢复，再重新扫描和计划。
- 内容校验可以拒绝已观察到的过期计划，但无法锁住任意编辑器或训练进程。写入期间停止两端编辑；同一对工作目录只使用一个控制器。
- 运行中的训练使用冻结的代码快照。更新开发工作树，不更新训练副本；本工具不会判断训练是否正在使用某个目录。
- 没有常驻守护进程。安装 Skill 不会让用户手工保存文件、第三方编辑器或任意远端 `git commit` 自动触发同步。

## Agent Skill

将 [`skills/worktree-bridge`](skills/worktree-bridge/SKILL.md) 复制到你的 Agent 支持的 skills 目录，并确保 CLI 在所用 Python 环境中可导入。Skill 说明何时检查两端、遇到分叉如何保全、怎样刷新上下文；确定性的状态判断和文件操作由 CLI 执行。

Skill 本身不授予 commit、push、启动训练或公开发布权限。用户本轮授权、项目工作规则和目录角色决定允许的操作。

## 设计与调研

- [GitHub 竞品与产品定位](docs/research.md)
- [协同模型、Git 操作边界与后续路线](docs/design.md)
- [验证记录与未验证范围](docs/validation.md)

运行测试：

```console
python -m unittest discover -s tests -v
```

代码采用 MIT 许可证。项目名为工作名称，尚未核查包名、商标或公共仓库名称的可用性。
