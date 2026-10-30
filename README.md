# team-dev

在 Codex 中协调三个角色：**Astra 负责拆解与验收，本地 OMP / DeepSeek 实现，原生 Sol 独立审核**。代码目标可以位于本机，也可以通过 SSH 位于远端服务器。

这是一套 Codex skill 和执行辅助脚本，需要已有的 Codex 原生子代理能力与本地 OMP。它不会把任意第三方模型注册成 Codex 原生模型，也不要求远端服务器安装 OMP。

## 工作流程

```text
Astra 定义任务、文件范围、接口和验收条件
  → 从本地或 SSH Git 目标捕获选定文件的快照
  → 本地 OMP / DeepSeek 修改快照并运行允许的检查
  → Astra 检查候选改动，以基线校验保护目标上的现有工作
  → 在真实目标环境运行约定测试
  → 原生 Sol 独立审核代码、测试与过程证据
  → Astra 核对验收账本并完成收尾
```

测试通过、工具结束和任务完成是不同状态。辅助脚本保留失败记录、限制恢复次数，并校验候选改动、测试与审核的绑定。详细操作以 [SKILL.md](SKILL.md) 和 [任务合同](references/task-contract.md) 为准。

## 环境要求

| 位置 | 要求 |
| --- | --- |
| Codex 主控 | 支持原生子代理，并能请求 `gpt-5.6-sol` / `max`；主控使用 Astra |
| 本地 worker | macOS 为已验证环境；Git、SSH、Python 3.11+（测试使用 3.12）、PATH 上的 OMP **18.2.8** |
| 模型服务 | 可用的 OpenAI Chat Completions 兼容接口，提供 `deepseek-v4.1-flash` 和工具调用能力 |
| SSH 目标 | 已配置的 SSH 别名与认证、Git、POSIX Python 3.9+、GNU `timeout` 及项目已有测试环境 |

严格 Bash 门禁按 OMP 18.2.8 固定校验，不能直接把“最新版 OMP”视为兼容。其他操作系统与 OMP 版本需要单独验证。模型服务的可用性、费用和额度由服务商决定；本仓库不附带账户或凭据。

默认 worker selector 是 `opencode-ds/deepseek-v4.1-flash`，历史模型兼容仅用于已有记录。更换 provider/model 需要同步检查 [模型约束](scripts/worker_model.py)，不能只改一个配置字段。Sol 的请求路由不等于已验证的后端模型身份；宿主不暴露实际身份时保留未知状态。

## 安装

目标目录尚不存在时：

```bash
git clone git@github.com:LightInDust/team-dev.git "$HOME/.codex/skills/team-dev"
```

如果已经安装过，先比较当前版本和本地修改，再自行合并更新。不要覆盖正在运行任务依赖的 skill 文件或私有配置。

仓库根目录就是 skill 目录。重新加载 Codex 的技能列表后，可用 `$team-dev` 指定使用本技能。Superpowers 的借鉴已包含在 [适配说明](references/superpowers-adaptation.md) 中，不要求安装其全套技能。

## 本地私有配置

配置文件位于仓库外。示例只有占位值，不可直接用于真实请求：

| 目标文件 | 参考模板 / 用途 |
| --- | --- |
| `~/.config/codex-team-dev/credentials.json` | [credentials.example.json](examples/credentials.example.json)，填写自己的 API key |
| `~/.config/codex-team-dev/runtime.json` | [runtime.example.json](examples/runtime.example.json)，填写可用 Python 3.11+ 的绝对路径；可选 `http_proxy` 使用自己的地址 |
| `~/.omp/profiles/codex-ds/agent/models.yml` | [models.example.yml](examples/models.example.yml)，填写自己的接口 API root |

配置目录权限使用 `700`，凭据文件权限使用 `600`。在本机编辑凭据，避免把密钥写进聊天、命令行参数、任务 JSON 或 Git。OMP 的 `apiKey` 保持 `OMP_OPENCODE_API_KEY` 环境变量引用；辅助脚本从本地凭据文件读取后仅传入 worker 环境。

`baseUrl` 填 API 根地址，不包含末尾 `/chat/completions`。代理是可选网络设置，按本机实际情况配置。不要复制他人的配置、代理地址或私钥。

为 `codex-ds` 保持专用 OMP profile；检查其重试、模型回退和扩展设置，避免把交互式主会话的配置直接混用。worker 的启动参数由辅助脚本生成。远端只接收选定代码和测试命令，不应收到模型凭据。

```bash
python3 -B "$HOME/.codex/skills/team-dev/scripts/team_dev.py" doctor
```

`doctor` 只检查本地入口和凭据文件是否存在，不证明接口可用、账户有余额或模型已正确响应。

## 使用

在 Codex 中提供真实项目和验收要求，例如：

> 使用 $team-dev 修复当前 Git 项目中的指定问题。先确认文件范围和测试命令；本地 DeepSeek 实现，Sol max 独立审核，由 Astra 验收。

> 使用 $team-dev 修改 SSH 别名 dev-server 上指定 Git 仓库的代码。OMP 留在本机，测试在远端执行；先确认目标状态与所需上下文。

需要手动准备任务时，以 [本地任务模板](examples/task-local.example.json) 为起点，替换所有路径和验收内容，并阅读 [任务合同](references/task-contract.md)。模板中的测试命令只是语法示例，必须换成该项目真正需要的检查。

辅助入口包括 `policy`、`prepare`、`start`、`status`、`diagnose`、`propose`、`apply`、`test`、`test-receipts`、`record-review` 和 `finalize`。测试收尾中断时，先用只读的 `test-receipts` 检查[逐次回执与未知结果](references/target-test-receipts.md)。依赖步骤必须先确认上一步成功；审核不能用占位 PASS 文件替代。此辅助脚本不会自动提交或推送代码。

## 验证本仓库

从仓库根目录，使用 Python 3.11+ 和 PATH 上的 OMP 18.2.8：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

测试包含真实 OMP 进程与本机 loopback 假服务的协议验证；不需要真实模型 API key，也不会通过这些 fixture 发起付费模型请求。运行环境需允许本地监听和子进程检查。缺少 OMP 或版本不符应作为测试失败处理，不能称为完整验证通过。项目业务测试、远端连通性与真实服务可用性需要另外验证。

## 文件与隐私边界

- `SKILL.md`：角色、流程和分场景导航。
- `scripts/`：执行、诊断、证据校验和回归测试。
- `references/`：接口验收、异步任务、恢复和独立审核规则。
- `assets/`：可复用的合同、审核和可视化示例。
- `examples/`：公开配置与任务占位模板。

运行记录默认写入 `~/.local/state/codex-team-dev/runs`，可能包含源码、命令输出、模型文本和目标路径，属于私有材料。发布时只提交经过审查的源码、文档与合成示例；不要上传整个运行目录或本机技能目录中的缓存。`.gitignore` 只是误提交防护，不能代替发布前检查与 Git 历史检查。

本项目沿用仓库既有的 [MIT 许可](LICENSE)，版权署名使用公开 GitHub 账号。第三方代码出处与 MIT 许可保留在 [OMP RPC provenance](scripts/vendor/PROVENANCE.md) 和 [许可原文](scripts/vendor/LICENSE-omp-rpc)。Superpowers 的借鉴来源在 [适配说明](references/superpowers-adaptation.md) 中标明。
