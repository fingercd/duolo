# 配置与命令

本地 CLI 需要 Python 3.10+，远端探针需要 Python 3.9+；两端需要 Git，本机使用已有 OpenSSH 认证与 known_hosts。状态目录放在仓库外，保存内容指纹、执行日志和备份，不能发布。

```json
{
  "local_root": "D:/work/my-project",
  "remote": {"kind": "ssh", "host": "gpu-dev", "port": 22, "root": "/home/researcher/projects/my-project"},
  "state_dir": "D:/worktree-bridge-state/my-project"
}
```

只接受明确的一对开发目录，不从文件夹同名自动决定配对。私有配置放在项目外；SSH host 可以用现有配置别名。不要在配置里放密码、token 或私钥内容。

```console
python -m worktree_bridge --config project.local.json status
python -m worktree_bridge --config project.local.json baseline
python -m worktree_bridge --config project.local.json plan --out D:/worktree-bridge-state/my-project/plan.json
python -m worktree_bridge --config project.local.json apply --plan D:/worktree-bridge-state/my-project/plan.json
```

`status` 和不带输出路径的 `plan` 读取项目；`baseline` 写私有状态；`plan --out` 写本地计划；`apply` 可写两端项目。命令输出 JSON；检查退出码与返回问题，不能只看有没有输出。

计划文件必须放在两个工作树之外的本机私有目录，例如上述 state_dir。不要在被同步仓库根生成 plan.json，也不要覆盖已有配置或 baseline 文件。

基线是“上次双方都一致的内容”，不是自动选出的服务器权威版本。三方判断：本地等于基线、远端变化时拉回；远端等于基线、本地变化时推送；两端同改但结果不同则冲突；两端相同则无需复制。原型对删除一律停止。

原型按字节比较，CRLF/LF 差异也属于差异。先检查 `.gitattributes` 与 checkout 设置，未经审阅不要批量转换。机器特有文档或忽略文件不在公共同步范围内时应明确告知。

安装时使用现有源码目录的 `python -m pip install .` 或由用户环境设置 `PYTHONPATH=<source>/src`。不安装时可运行 `python <source>/scripts/bridge.py ...`，它调用同一核心。具体安装位置属于机器配置，不写进通用 Skill。

## Git 协调

当前 CLI 只检查 Git，不自动协调历史。两端同一工作文件可以有不同 HEAD、分支、暂存内容；这些状态不能靠文件复制同步。

Git 操作前先暂停任何文件同步，记录两端 HEAD、分支和未提交修改，按项目规定保全各自修改。若用户已授权提交或更新 Git 历史，使用 Git 原生 fetch/push/merge 等机制处理；不擅自扩大为 commit、stash、reset 或 force push。首次历史分叉的合并选择需要用户当前目标或项目明确规则，不能按“远端通常更新”推断。

Git HEAD/分支发生变化后，即使两端已经变到同一新提交，也不能沿用旧内容基线解释改动。两端重新达到预期相同提交和所选工作文件后，可显式 `baseline --refresh`，保留旧基线并记录新的共同状态。刷新只能在双方完全一致时进行，不能用来绕过冲突。当前原型不会自动完成 Git 协调步骤，不要把发现分叉写成“已修复分叉”。

## 失败恢复

一个文件的原子替换不等于整批事务。连接断开或某一步报错时，部分文件可能已经完成。读取状态目录中的执行记录和目标旧内容备份，重新扫描两端，确认实际完成项；不要盲目重放旧计划，不自动回滚仍在变化的文件，不自行编辑基线。

## 交付给下一位 Agent

记录具体配对、两端 HEAD/分支、扫描时间、计划/执行结果、仍未处理冲突、已重新读过的关键文档。记录运行实验的冻结 SHA、产物路径和摘要；不要把机器上的会话存储、凭据或全部日志塞进项目 AGENTS.md。
