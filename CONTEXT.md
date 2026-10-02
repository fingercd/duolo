# 当前实现与验证范围

更新：2026-10-02，版本 0.4.0。Duolo 是面向明确一对开发工作树的实验工具，主命令为 `duo`，Python 包与模块为 `duolo`，公开仓库为 `fingercd/duolo`。0.4 统一产品名称与传播物料，沿用 0.3 的注册与 CLI/MCP 工作流，无独立前端。

## 当前入口

在已有 Git 仓库运行 `duo init --remote gpu-dev --path /home/researcher/projects/my-project` 注册配对，再用 `duo start` 启动本机后台服务。注册不执行 `git init`、不提交、不覆盖文件、不立即同步。配置保存在本工作树的 Git 私有目录，注册索引和默认状态位于本机用户状态目录；后续命令从仓库或子目录自动发现配对。旧 JSON 可用 `init --from-config` 迁移，`--config` 保留显式覆盖。

已有配对保留 `worktree-bridge/config.json` 和 `WorktreeBridge` / `worktree-bridge` 用户状态布局，以继续发现原配置、基线和记录；`X-WTB-*` 本机协议也保留兼容。它们是内部兼容名称，使用新命令不需要搬动状态或重新建立基线。公开 Skill 的当前入口为 [skills/duolo/SKILL.md](skills/duolo/SKILL.md)。

`status` 默认读后台缓存并返回 JSON，`--short` 给出简短文本；服务未启动时失败并提示启动。`watch` 持续显示状态变化和事件。`status --fresh` 保留旧全量扫描入口。`sync` 请求同步，`wait` 发起只读新观察并等待确认一致，不能用保存前的绿色缓存满足等待，也不绕过暂停或关闭自动同步。

后台通过持久本地/SSH peer、增量观察和定期重新核对处理文件变化。第一次基线仅在 HEAD、具名分支和所选文件逐字节一致时建立；冲突、过期选择、历史分叉、独立暂存工作或活跃 Git 操作阻止相应写入。删除默认关闭；网络和检查失败明确返回状态，过期观察变为 `checking`。

## Git 与 Agent

自动 Git 跟进处理任一端已有 commit，使用原生对象传输和受保护快进；不自动 commit、merge、rebase、stash、强推或发布项目。历史传输前审核新增提交，包括中间版本文件，限制为 128 个提交、4096 个逐提交路径条目、8 MiB bundle。显式 `checkpoint` 在两端一致后从本地创建所选工作的 commit/tag，再让远端跟进；它不运行 `git commit` hooks。

CLI、公开 Skill 和可选官方 MCP SDK 适配器共享同一配对服务。MCP 提供固定项目状态、同步、暂停、恢复、冲突列表、显式冲突选择、等待和 checkpoint；不接受任意 shell 或替换项目根。同步项目规则之后，Agent 仍须重读 `AGENTS.md`、`CONTEXT.md` 及必要文档。

旧 `baseline`、`plan`、`apply` 保留显式计划工作流。活跃服务与旧写命令通过控制器锁互斥；不能用刷新基线绕过差异。原项目、其他分支与训练快照不会因为相同 origin 自动加入配对。

## 验证与历史证据

2026-10-02 记录的 0.3 验收完成了本机隔离集成、注册 CLI 与正式 MCP SDK 接入测试，以及专用隔离副本上的真实 Windows→Linux 持久 SSH 端到端验收，覆盖观察屏障、缓存状态、两端文件编辑、暂停与冲突选择、创建/重命名/删除、远端原生提交跟进及 checkpoint/tag。这些是更名前的日期化证据，不是 0.4 新测量；具体结果、性能和失败场景由 [验证记录](docs/validation.md)维护。

0.2 的真实隔离 SSH 双向写入验收是已保留的历史证据，覆盖当时的显式计划路径、冲突停止、过期计划拒绝和失败恢复。0.2 还修正过 Git 环境变量污染、祖先链接、不完整目录扫描和 SSH alias 端口覆盖；旧 0.1 SSH 基线身份不足时，保留旧状态并另建专用状态，不编辑旧基线冒充同一端点。

首次接入仍须分别保全原工作。独立开发副本可以从相同提交建立，仅对新副本约定换行，再迁入选定的未提交工作；原目录与环境保留。训练使用冻结快照，本工具不检查训练进程，也不负责启动训练。

设计见 [docs/design.md](docs/design.md)，使用见 [README.md](README.md)、[配置](docs/configuration.md)和[MCP](docs/mcp.md)。[调研](docs/research.md)记录早期方案比较，不能替代当前版本能力与验证结论。
