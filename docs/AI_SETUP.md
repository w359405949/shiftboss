# 由 AI 代装 shiftboss

本文给代用户安装 shiftboss 的 AI 编程助手读。按顺序执行；每步的「核对」不成立就停下，把原始输出告诉用户，不要绕过。

## 0. 先问清楚

开始前向用户确认以下三项，得到答复再动手：

| 项 | 要确认的内容 |
| --- | --- |
| 项目 | 要启用 shiftboss 的 git 仓库路径（下称「项目根」）；必须是 git 工作树 |
| 派活方 | 用哪个 AI 当派活方（通常是 Claude Code）；后面的钩子按它配置 |
| 执行方 | 用哪个编程代理当执行方（缺省 Codex）；须已安装并登录，shiftboss 不负责安装或登录它 |

## 1. 检查前提

```bash
python3 --version   # 须 ≥ 3.11
git --version
codex --version     # 执行方是 Codex 时
```

核对：三条命令都有输出且 Python 版本达标。

## 2. 取得代码并安装

把 shiftboss 克隆到项目根的**旁边**（与项目同级），在同级目录建虚拟环境，以可编辑模式安装：

```bash
cd <项目根的上一级>
git clone https://github.com/w359405949/shiftboss.git
python3 -m venv .venv
.venv/bin/pip install -e shiftboss
```

让 `shiftboss` 命令在 PATH 上可用（激活该虚拟环境，或把 `.venv/bin/shiftboss` 链接到用户已在 PATH 上的目录）；改用户的 shell 配置前先征得同意。

核对：在项目根运行 `shiftboss --help` 列出子命令。

## 3. 项目配置

项目根没有 `shiftboss.toml` 时新建一份，只写已向用户确认的项，其余留缺省；各项含义见 [DESIGN.md](../DESIGN.md) 第 6 节。最小可用的写法：

```toml
[workers]
default = "codex"
```

## 4. 启用

在项目根运行（会在项目旁建 `<项目名>.shiftboss/` 家目录，并在项目的 `CLAUDE.md`／`AGENTS.md` 里追加一行指向流程文档；运行前把这两点告诉用户）：

```bash
shiftboss init
shiftboss bootstrap --check
```

核对：`bootstrap --check` 不报缺项。

## 5. 派活方钩子（派活方是 Claude Code 时）

把下列钩子写进**包含项目根的工作区**的 `.claude/settings.json`（已有文件就合并，不覆盖用户原有设置）。`<PY>` 换成第 2 步虚拟环境里 python 的绝对路径，`<ROOT>` 换成项目根的绝对路径：

| 事件 | 命令 | 作用 |
| --- | --- | --- |
| `Stop` | `cd <ROOT> && <PY> -B -m shiftboss.hooks.show_brief` | 每次回复结束显示状态摘要（待定事项、在办、门铃、验证） |
| `UserPromptSubmit` | `cd <ROOT> && <PY> -B -m shiftboss.hooks.claim_dispatcher` | 用户发话的会话认领派活方 |
| `PreToolUse`（matcher `Bash`） | `<PY> -B -m shiftboss.hooks.no_session_probing` | 判断执行方状态只走收件箱 |
| `PreToolUse`（matcher `Bash`） | `<PY> -B -m shiftboss.hooks.no_test_suites` | 派活方不跑测试套件与生成 |
| `PreToolUse`（matcher `Bash`） | `<PY> -B -m shiftboss.hooks.bell_standalone` | 门铃只能单独后台挂起 |
| `PreToolUse`（matcher `Bash`） | `<PY> -B -m shiftboss.hooks.merge_standalone` | 合并单独一条命令 |
| `SessionStart`（matcher `resume`） | `cd <ROOT> && <PY> -B -m shiftboss.cli recover` | 恢复会话时接回进程没了的执行方 |

另在同一文件的 `env` 里设 `"BASH_MAX_TIMEOUT_MS": "21600000"`，让门铃能挂满时限。

核对：新开一个派活方会话，回复结束时能看到以「【时间】」开头的状态摘要。

## 6. 交给用户

告诉用户：

1. 安装位置（shiftboss 克隆、虚拟环境、家目录）与改过的文件清单。
2. 以后在项目根开派活方会话，直接提需求即可；其余 shiftboss 命令由派活方自己调用。

## 不要做

- 不向任何远端推送（项目仓库与 shiftboss 仓库都不推）。
- 不替用户安装或登录 AI 代理，不改系统级配置。
- 检查失败不改检查、不加跳过，停下报告原文。
