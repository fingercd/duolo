# Duolo 的隔离 SSH 验收

`scripts/ssh_acceptance.py` 是明确启用的写入测试，不包含在默认联网测试中。它只接受带专用标记的隔离 Git 工作树，不负责创建、删除或反复重试测试环境。不要把研究项目、发布目录或运行中的训练目录用作 fixture。

本页说明沿用 0.2 显式计划验收脚本的隔离协议。Duolo 0.4 保留 `wtb-acceptance` 分支、目录前缀和 `.wtb-acceptance.json` 标记，以继续识别旧 fixture；它们不是当前主命令 `duo`。0.3 后台持久 SSH 的历史验收与测量见[验证记录](validation.md)，不能把本脚本的历史结果当成新版本复测。

## 准备测试副本

先在项目外创建一个初始 Git 仓库，分支为 `wtb-acceptance`，提交三个普通UTF-8文件：`AGENTS.md`、`CONTEXT.md`、`.wtb-acceptance.json`。标记文件内容为：

```json
{"schema":1,"purpose":"wtb-acceptance","fixture_id":"wtb-acceptance-UNIQUE_ID"}
```

通过 Git clone 或 Git bundle 在 SSH 服务器创建同一个提交的副本；不要复制 `.git` 文件系统目录。两边根目录 basename 都必须以 `wtb-acceptance-` 开头，根路径不能经过符号链接。新测试副本使用一致换行，避免初始字节差异；不改全局 Git 换行设置。

私有配置沿用常规 `local_root`、`remote`、`state_dir`，额外设置 `acceptance`：

| 字段 | 值 |
|---|---|
| `fixture_id` | 标记文件中的唯一 ID |
| `expected_head` | 两个副本共同的完整 Git SHA |
| `expected_branch` | `wtb-acceptance` |
| `marker_sha256` | 标记文件原始字节的 SHA-256 |
| `initial_hashes` | 恰好包含 AGENTS.md、CONTEXT.md 的原始字节 SHA-256 |

state_dir 的 basename 同样以 `wtb-acceptance-` 开头，必须为空或尚不存在。配置、状态和报告都位于工具源码与测试工作树之外；报告不得放在 state_dir 内，不得复用已有报告文件。每轮测试使用新的状态和报告，失败后保留现场。

```console
python scripts/ssh_acceptance.py --config /absolute/private/fixture.json --report /absolute/private/result.json
```

## 检查步骤

脚本核验端点、marker、Git 版本、受支持文件字节一致和干净状态，再依次执行：

1. 建立共同基线。
2. 本地修改 AGENTS.md，通过核心 plan/apply 推到服务器。
3. 服务器修改 CONTEXT.md，通过核心 plan/apply 拉回本地。
4. 两端把同一文件改成不同内容，确认 plan 与 apply 拒绝且没有覆盖。
5. 生成计划后再修改文件，确认旧计划被拒绝。
6. 显式恢复测试内容后，通过新计划重新完成同步。

报告持续保存已通过步骤、两端文件指纹、计划 ID 和 journal 位置。失败返回非零，并尝试只读采集现场；不会重放写入、清除 fixture 或注入实际网络断线。

## 0.2 的额外受控验证

独立验证在真实 SSH 写入返回后，故意由本地客户端丢弃一次成功回执，模拟“服务器已写入，但调用者不知道”的情况。预期结果是 journal 标记中断、旧基线保持不变；重新扫描后，新计划只包含剩余文件，最终两端一致。

这一实验不是真实断网、停机或磁盘掉电测试，不能据此宣称已覆盖所有网络/崩溃故障。它验证的是当前恢复原则：先观察实际状态，再生成新计划，不盲目重放旧批次。

Linux另行验证了祖先符号链接与不可读未跟踪子目录。Git 遇到无法打开的目录可能只给warning并返回0，工具仍必须拒绝把这次不完整扫描记录为共同基线。
