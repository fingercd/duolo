# Duolo 的早期调研与产品定位

调研日期：2026-10-02。依据公开仓库 README、源码、官方文档与部分 issue，未安装并运行全部竞品。以下是定向样本，不能用“本次未找到”证明全网没有同类项目；项目的宣传范围也不等于经过独立验证的可靠性。

本文保留更名前 Worktree Bridge 0.1 / 0.2 原型阶段的调研判断；下文“当前原型”“本轮”及后续路线均指当时的实现，不能作为 Duolo 0.4 能力清单。现有注册、后台同步、Git 跟进与 MCP 边界见 [README](../README.md)和[设计说明](design.md)，日期化验收见[验证记录](validation.md)。

## 结论

本地 Agent、远端算力和文件同步已有成熟组件及直接相邻方案。新项目不应定位为重新发明 Git 或通用同步算法。较合理的切口是：**面向 Windows 本地 Agent 与 Linux GPU 开发者，将具体的一对工作树、各自 Git 状态、项目规则和同步计划放在同一个可检查的流程中**。

最接近的直接参考是 `claude-remote-shell`，不是只会生成规则的工具。它已实现本地 Agent、远程命令和双向项目文件同步。但其当前脚本选用 Mutagen `two-way-resolved`，冲突由本地端胜出，与本项目“冲突停下”的要求不同。生产版宜复用成熟同步组件，在外层增加身份/版本检查与 Agent 工作流；本轮标准库 CLI 只是验证流程和边界的保守原型。[项目](https://github.com/torarnv/claude-remote-shell#readme)、[同步脚本](https://github.com/torarnv/claude-remote-shell/blob/main/claude-remote-shell)、[Mutagen 模式定义](https://mutagen.io/documentation/synchronization/#modes)。

## 主要方案比较

| 项目 | 实际机制与原有用户 | 已解决的痛点 | 与本需求的关系 |
|---|---|---|---|
| [Mutagen](https://github.com/mutagen-io/mutagen) | 面向远程开发，文件监控、三方状态、差分传输，支持 SSH 与双向模式 | 本地编辑后远端快速可运行，支持冲突状态和暂停/刷新 | 最接近成熟文件同步内核。需要另做明确目录配对、Git 状态检查、Agent 刷新与训练目录策略；不能直接把持续同步两个 checkout 解释为 Git 同步 |
| [Unison](https://github.com/bcpierce00/unison) | 两个副本之间的双向文件同步，记录上次共同状态 | 两端都编辑时区分单侧改动与冲突，支持 SSH | 适合“遇到冲突交给用户”的文件层；不定义研究项目的运行快照与 Agent 流程 |
| [Syncthing](https://github.com/syncthing/syncthing) | 多设备持续同步，可生成冲突副本 | 离线与多设备文件交换 | 更偏通用设备文件同步。冲突副本保住内容不代表当前工作树可运行；不应持续同步活跃 `.git` |
| [claude-remote-shell](https://github.com/torarnv/claude-remote-shell) | Claude Code 的 Bash 经 SSH 远端执行，本地读写文件；可选 Mutagen 双向项目同步，命令前后 flush | 本地 Agent 与远端 GPU/构建环境结合 | 最接近本地 Agent + 远程运行。现有脚本采用本地赢冲突模式；README 没有承诺 Windows 原生通用支持，也警告 Git worktree 绝对路径问题 |
| [agentctl](https://github.com/qtnx/agentctl#remote-usage) | 将本地项目 rsync 到远端，在远端 Docker 执行命令并回传输出 | Agent 远端隔离执行与持续推送本地变化 | 方向偏本地推送；README 排除 `.git`/`.worktrees`，没有提供两端独立修改后的 Git 历史协调合同 |
| [codex-workspace-sync](https://github.com/Companionh/codex-workspace-sync) | Windows-first 客户端与 Linux 自托管服务，Codex 上下文、共享 skills/docs 和运行状态同步，有 lease | 在设备间携带 Codex 工作上下文 | 是 Windows + Linux + Agent 上下文的直接相邻项目。README 标为实验 alpha；核心合同是 Codex 状态，不是任意项目源码工作树；默认同一时刻单个活动设备 |
| [agent-sync](https://github.com/lidongpeng36/agent-sync) | SSH/rsync，同步 Codex、Claude、OpenCode 会话、索引与 memory，含部分 skills/摘要 | 跨机器继续 Agent 对话和记忆 | 可参考其计划、清单、备份和事务检查；操作边界是特定 Agent 状态适配器，不是一般源码目录。README 未给出 Windows 支持承诺 |
| [ClaudeSync](https://github.com/jyshnkr/claudesync) | SSH/rsync 推拉 Claude 配置、会话目录和项目说明，提供预览与备份 | Claude 使用环境和部分上下文迁移 | 与项目说明同步有重叠，但不是源代码和两个 Git checkout 的协调器 |
| [Ruler](https://github.com/intellectronica/ruler) / [Rulesync](https://github.com/dyoshikawa/rulesync) | 以共同规则源生成不同 Agent 的规则、skills、MCP 等配置 | 同一项目使用多个 Agent 时避免手工维护多份规则 | 是格式与配置分发，不是 SSH 跨机器工作树同步。可在同步工作树内部继续使用 |
| [dot-agent](https://github.com/cthulhu/dot-agent) | 用 Git 同步多设备上的用户级 Agent 配置 | 全局规则和工具配置随设备迁移 | 更接近 dotfiles；与项目代码版本和训练副本是不同对象 |
| [VS Code Remote-SSH](https://code.visualstudio.com/docs/remote/ssh#_working-with-local-tools) | 直接打开远端工作区，编辑与调试使用远端环境 | 避免两份开发代码和环境不一致 | 如果用户接受 Agent 也在远端工作区操作，这是更简单的选择。官方说明本身不直接提供本地源码同步；本需求明确保留本地 Agent 工作树，因此仍有复制问题 |

补充：`rsync` 是良好的单向传输组件，但不会自行建立双向共同基线；两次相反方向 rsync 不能自动得到冲突安全的双向同步。`lsyncd` 监听并调用 rsync 建立单向镜像，README 明确不适合对称双向镜像。[rsync 手册](https://github.com/RsyncProject/rsync/blob/master/rsync.1.md)、[Lsyncd 说明](https://github.com/lsyncd/lsyncd#2-waybidirection-synchronization)。

自动 Git 工具也不是空白市场。[gitwatch](https://github.com/gitwatch/gitwatch) 监控改动并自动提交、可选推送；[git-sync-rs](https://github.com/colonelpanic8/git-sync-rs) 和 [git-auto-sync](https://github.com/OctopusGarage/git-auto-sync) 的 README 描述了自动提交、拉取/推送或合并等流程。它们减少 Git 操作步骤，但以提交历史为单位，也会引入自动 commit/rebase 的产品选择。本项目不默认代替用户创建提交。

## 最关键的技术反证

### 工作文件相同，Git 状态仍可能不同

两个 checkout 各自保留 `.git` 时，一端提交后另一端并不会收到该提交；相同文件可分别显示为“已提交”和“未提交”。Mutagen 的官方 VCS 文档明确讨论了这种现象，并建议只在一端管理 VCS。其理由还包括 index 与本机文件系统相关、对象库可重打包，以及 Git 不接受绕过它的并发元数据写入。[Mutagen VCS 文档](https://mutagen.io/documentation/synchronization/version-control-systems/)。

因此，本项目的 Git-aware 只应表示“读取并检查 Git 状态、分叉时停止”，不能偷换为“已同步 Git 历史”。若后续要支持两端提交，需要独立设计 Git 协调协议。

### “两端都能改”还需要明确冲突政策

Mutagen 默认 `two-way-safe` 会记录无法安全消解的冲突；`two-way-resolved` 则允许 alpha 端赢得冲突。两者都可叫双向同步，但保证不同。`claude-remote-shell` 当前源码采用后者，这使“我们能遇到冲突就停”成为可验证的区别，而不是泛泛说“竞品不懂 Agent”。[同步模式](https://mutagen.io/documentation/synchronization/#modes)、[claude-remote-shell 源码](https://github.com/torarnv/claude-remote-shell/blob/main/claude-remote-shell)。

本项目要求整批存在冲突就不开始写，不能仅把 Mutagen 的 safe 模式理解为“任何冲突都会中止整个 session”；两者的执行语义还需在适配器中验证。

### 同步 `.git` 已有实际故障案例

Syncthing 社区有 `.git/index`、HEAD 和 refs 产生冲突副本的案例，维护者建议让 Git 自己做 fetch/push 或排除 `.git`。这是具体的兼容性问题，不等同于“Syncthing 不可靠”。[官方论坛案例](https://forum.syncthing.net/t/resolving-sync-conflicts-in-git-folder/11969)。

### 会话与规则同步不会自动刷新正在运行的 Agent

Ruler/Rulesync 处理规则配置生成；agent-sync/Codex Workspace Sync 处理各自定义的 Agent 状态。将更新后的 `AGENTS.md` 放到磁盘，不足以证明正在运行的 Agent 已经重新加载它。新的工作流必须主动读变化的文档，并在交接中记录已观察的版本；具体宿主是否会自动重载需按宿主核验。

### MCP 是接口，不是同步算法

MCP 为应用暴露 tools、resources 和 prompts。做一个 SSH MCP 可以让 Agent 操作服务器，但不会自然获得共同基线、冲突判断和恢复日志。适合的实现是让 CLI 与 MCP 共享同一个已测试的同步核心。[MCP 官方架构](https://modelcontextprotocol.io/docs/learn/architecture)、[SSH MCP 示例](https://github.com/taigrr/ssh-mcp)。

## 开源切口与用户范围

优先用户：Windows 本地使用编码 Agent、Linux/HPC/GPU 上执行与偶尔改代码、已有多个 checkout/运行快照、又不希望每次试一个小改动都先 Git push 的个人研究者与小团队。

差异化应可以用功能验收，而不是口号：

1. 显式配对工作目录，并同时显示 Git HEAD/分支和文件内容差异。
2. 让同一 CLI 可以供人、不同 Agent 和后续 MCP 使用。
3. 对代码、公共项目文档使用同一基线，冲突时整批拒绝；机器配置和训练产物保留各自位置。
4. 对“计划已过期”“文件相同但 Git 不同”“文档到达但上下文未刷新”给出不同状态。
5. 在命令前后可验证一致性，保留失败日志与被覆盖内容的备份。

第一版不覆盖多人实时协作编辑、通用云盘、大模型数据管道或全量 Agent 会话迁移。若只需要两个目录自动一致，优先直接使用成熟同步软件；本项目必须凭额外的工作流正确性证明维护成本值得。

## 后续技术选择

保留现在的手动原型用于演示、回归和定义行为。生产级持续同步评估 Mutagen 或 Unison，避免再实现跨平台 watcher、差分传输和可靠重连；但两者都需要实测外层 Git 检查与其持续写入之间如何暂停和串行化，不能只套一条命令就宣称解决。

若需要 MCP，先暴露 `status`、`plan` 等只读操作，再让写操作引用已保存计划和最新状态。常驻 watch、Git 协调、Agent handoff 和训练冻结快照应分别提供清楚的能力声明。当前原型没有完成这些后续能力。
