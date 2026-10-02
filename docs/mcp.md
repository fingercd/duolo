# Duolo 的可选 MCP 接入

MCP 适配器让 Agent 用固定工具查询和操作已注册项目。它复用 CLI 的配对控制器，不自动启动服务；普通 CLI 与后台同步无需安装 MCP SDK。

## 安装与启动

在源码目录为相同 Python 环境安装 extra，使用官方 SDK `mcp>=1.28,<2`：

```console
python -m pip install ".[mcp]"
```

也可从 GitHub 安装固定版本：

```console
python -m pip install "duolo[mcp] @ git+https://github.com/fingercd/duolo.git@v0.4.0"
```

进入已注册仓库，先启动服务，再运行 stdio 适配器：

```console
cd D:/work/my-project
duo start
duo mcp
```

stdio 的标准输出用于协议，不是普通交互界面。适配器固定绑定启动时发现的项目；不会在每次工具调用时切换到其他目录。

## 客户端示例

客户端若支持为 MCP 进程指定工作目录，可在已注册仓库中启动 `duo mcp`。下方使用显式配置兼容方式，适用于不能稳定设置工作目录的客户端；`command` 填安装了本工具与 extra 的 Python 解释器绝对路径。

```json
{
  "mcpServers": {
    "duolo": {
      "command": "D:/work/duolo-env/Scripts/python.exe",
      "args": [
        "-m", "duolo",
        "--config", "D:/work/bridge-config/project.json",
        "mcp"
      ]
    }
  }
}
```

字段和配置位置以客户端为准。多个项目使用各自明确命名的 MCP 条目；真实路径与地址留在私有配置中。

新注册项目无需另造一份 JSON 配置：把示例中的 `--config` 路径替换为 `duo init` 返回的 `config_path` 即可。普通仓库通常位于 `D:/work/my-project/.git/worktree-bridge/config.json`；linked worktree 以返回值为准。

`worktree-bridge` 是旧配对的兼容存储名称，0.4 不迁移它；MCP 的显示条目名、主命令和 Python 模块已使用 Duolo / `duo` / `duolo`。本机控制协议保留 `X-WTB-*` 请求头，不需要在客户端另设一套协议。

## 工具与完成语义

| 工具 | 参数 | 行为 |
|---|---|---|
| `project_status` | 无 | 读缓存状态、不一致、观察时间、连接与错误。 |
| `sync_now` | 无 | 请求立即核对和受检查的同步。 |
| `pause_sync` / `resume_sync` | 无 | 暂停 / 恢复自动同步；已开始动作可能完成。 |
| `list_conflicts` | 无 | 返回冲突与当前 `revision`。 |
| `resolve_conflict` | `path`, `choice`, `expected_revision` | 为相对路径选择 `local` 或 `remote`，覆盖另一端；版本必须仍有效。 |
| `wait_until_synced` | `timeout=30.0` | 发起只读观察，等待本次核对完成且状态一致。 |
| `checkpoint` | `message`, 可选 `tag` | 两端先一致后，从本地创建所选工作的 commit/tag 并让远端跟进。 |

结果提供 JSON 文本与结构化内容。`queued: true` 表示接受，不表示完成；用后续状态或等待核对。`wait_until_synced` 不传文件、不执行 Git 跟进、不绕过暂停或关闭自动同步的策略，保存之前的绿色缓存不能满足这次等待。离线、观察失败与超时返回错误或实际未同步状态。

Agent 应先读 `project_status`，检查 `observed_at`、连接、`checking` 和阻止项；必要时请求 `sync_now` 再等待。成功后重新读取变化的项目规则。冲突选择前审阅两端内容，并用刚取得的 `revision`；超时后不盲目重放可能已经部分执行的写动作。

## 操作范围

适配器不接受任意 shell、URL 或替换项目根，通过固定配置身份把动作交给同一串行控制器。写工具仍遵守用户授权和项目约定，安装 MCP 本身不授予 commit、push、训练或发布权限。

`checkpoint` 不运行 `git commit` hooks，依赖 hooks 的检查需先按项目要求单独执行；也可手动提交后让服务跟进。自动 Git 传输审核新增历史，限制为 128 个提交、4096 个逐提交路径条目、8 MiB bundle，遇到排除文件或超限会停止。MCP 接口限制缩小操作范围，不是针对不受信任本机程序的隔离沙箱。
