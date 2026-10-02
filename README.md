<div align="center">

<img src="docs/assets/brand.svg" alt="Duolo" width="88" />

# Duolo

**本地与 SSH 两端开发：同步文件、跟进 Git，让 Agent 随时查询。**

[**中文**](README.md) | [English](README.en.md)

[开始使用](#开始使用) · [Agent 接入](#让-agent-使用) · [文档](#文档与帮助)

![Python 3.10+](https://img.shields.io/badge/local-Python%203.10%2B-blue)
![Experimental 0.4.0](https://img.shields.io/badge/version-0.4.0%20experimental-orange)
![MIT License](https://img.shields.io/badge/license-MIT-green)

</div>

在本地用编辑器或编码 Agent 改代码，也在 SSH 服务器上调试和运行？**Duolo 连接两端的 Git 开发目录，把文件同步、版本跟进和 Agent 可查询的状态放进同一套流程。**

未提交的源码和项目文档可以双向更新；一端创建提交后，另一端在条件允许时跟进。你和 Agent 都能查看文件是否一致、Git 是否跟上，以及哪里需要处理。

**15 秒概念演示** · 本地与 SSH 两端协作

https://github.com/user-attachments/assets/c69f531f-c31a-4bcc-bc56-32c1617054a3

## 为什么用 Duolo

- **未提交的改动也能同步。** 本地或服务器保存源码、项目文档后，单侧修改自动传到另一端。
- **文件和 Git 一起协调。** 同步工作中的内容，也在保护条件满足时跟进已有提交；不会每次保存都自动 commit。
- **人和 Agent 查看同一份状态。** 文件、Git、连接和冲突一起查询，可在终端查看，也可通过 MCP 接入编码 Agent。
- **需要处理的问题明确报告。** 文件冲突、历史分叉或网络问题会指出原因，由你审阅后决定下一步。

## 开始使用

本机需要 **Python 3.10+、Git 和 OpenSSH**；服务器需要 **Python 3.9+ 和 Git**。先确认已有 SSH 密钥登录可用，目标主机密钥已受信任。

**1. 安装 Duolo**（从 GitHub 安装，尚未发布到 PyPI）：

```console
python -m pip install "duolo @ git+https://github.com/fingercd/duolo.git@v0.4.0"
```

**2. 进入你已有的项目，告诉 Duolo 服务器上的对应目录：**

```console
cd D:/work/my-project
duo init --remote gpu-dev --path /home/researcher/projects/my-project
```

`gpu-dev` 是你的 SSH 主机名或配置别名，替换成实际目标；目录也换成自己的项目路径。`init` 只记住这两个目录，启动后才开始同步。

**首次使用前，两端须在同一个 Git 提交、同一个分支，准备同步的文件内容也要相同。** 已经有不同修改的项目，先按[首次接入说明](docs/onboarding.md)保留并整理两边的工作。

**3. 启动并查看结果：**

```console
duo start
duo status --short
```

现在可以继续编辑。Duolo 在后台同步，Windows 上不会弹出额外控制台窗口。之后在这个项目或其子目录里直接使用 `duo`，不必反复填写服务器路径。

## 常用命令

| 命令 | 做什么 |
|---|---|
| `duo status --short` | 查看文件、Git、连接和冲突状态。 |
| `duo watch` | 持续显示状态变化，Ctrl+C 退出查看。 |
| `duo sync --wait` | 立即同步，并等待确认结果。 |
| `duo pause` / `duo resume` | 暂停 / 恢复自动同步。 |
| `duo checkpoint -m "experiment setup" --tag exp-001` | 明确保存一次 Git 提交，并给它加上可选标签。 |
| `duo stop` | 停止当前项目的后台同步。 |

`duo status` 默认返回 JSON，适合脚本和 Agent；`--short` 适合人看。状态来自后台最近一次检查，断线或检查过期也会显示出来。更多选项用 `duo --help`，配置与状态说明见[文档](docs/configuration.md)。

## 让 Agent 使用

可选 MCP 接口让编码 Agent **查询与 CLI 相同的文件、Git 和连接状态**，并请求同步、等待新的检查结果、处理冲突或保存一次提交。

安装 MCP 支持后，在已经连接好的项目里启动适配器：

```console
python -m pip install "duolo[mcp] @ git+https://github.com/fingercd/duolo.git@v0.4.0"
duo mcp
```

按[MCP 接入说明](docs/mcp.md)添加到你的 Agent 客户端。也可安装[项目 Skill](skills/duolo/SKILL.md)，帮助 Agent 在同步后重新读取变化的项目规则。

## 使用范围

**0.4.0 实验版本。** 已完成本机测试、正式 MCP SDK 测试和真实 Windows ↔ Linux SSH 验收；具体结果见[验证记录](docs/validation.md)。

- 同步源码和项目文档，训练数据、模型权重及常见产物不在同步范围内。删除默认关闭；完整范围见[配置](docs/configuration.md)与[设计说明](docs/design.md)。
- Git 历史分叉、文件冲突或网络问题会报告出来。Duolo 不自动合并冲突、不向 GitHub 推送项目，也不保证任意同时编辑都不会丢失修改。
- 正在运行的训练使用固定代码副本。同步只连接你选定的两个开发目录，不接管其他项目或运行快照。

## 文档与帮助

[首次接入](docs/onboarding.md) · [配置与状态](docs/configuration.md) · [MCP](docs/mcp.md) · [实现与限制](docs/design.md) · [验证记录](docs/validation.md)

发现问题或有建议，欢迎提交 [Issue](https://github.com/fingercd/duolo/issues)。附上复现步骤，分享日志前去掉真实主机、私有路径和秘密内容。

从源码开发：

```console
git clone https://github.com/fingercd/duolo.git
cd duolo
python -m pip install -e .
python -m unittest discover -s tests -v
```

采用 [MIT 许可证](LICENSE)。
