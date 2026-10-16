# Shiftboss 设计

把 `workflow/` 整理成可独立安装的 Python 包 `shiftboss`：一个 AI 派活方（shift boss，当班工头）把任务书派给多个编程代理（执行方）轮班干活，靠 git 推送唤醒、靠常驻验证兜底，全部状态从 git 与少量台账现算。

## 1. 术语

| 术语 | 含义 | 现名 |
| --- | --- | --- |
| 任务书（taskbook） | 一件事的合同、轮次交代与交回 | 同 |
| 线（line） | 一份任务书在一个工作副本、一个分支上的执行 | 同 |
| 班（shift） | 线的一轮：派出到交回 | 轮 |
| 派活方（boss） | 写任务书、派活、核对、合并的一方 | 派活方／甲方 |
| 执行方（worker） | 在工作副本里干活的编程代理 | 执行方／乙方 |
| 门铃（bell） | 推送与验证结果唤醒派活方 | 同 |
| 常驻验证（night shift） | 在独立副本里对主线做生成、测试与发布 | 同 |
| 控制目录（control dir） | 只供派活方读写的私有文档、台账与记录 | 副本目录下的 `control/` |

## 2. 包结构

```text
shiftboss/                   # 包仓库（本仓库），以可编辑模式安装
  pyproject.toml  DESIGN.md
  src/shiftboss/
    cli.py                   # 单一入口：shiftboss <子命令>
    config.py paths.py       # 读 shiftboss.toml；项目根按当前目录所在工作树认
    taskbook.py state.py     # 任务书布局与解析；控制目录内的台账读写
    inbox.py dispatch.py merge.py doorbell.py recover.py bootstrap.py
    regen.py outputs.py      # 分层生成与产物声明
    verify.py verify_tests.py   # 常驻验证
    workers/                 # 执行方插件：codex、agy、omp
    native.py scripts/       # 平台适配与原生脚本（windows/*.ps1、posix/*.sh）
    hooks/                   # 提交守卫与派活方会话钩子
    docs/                    # 通用流程规则与任务书骨架（源）
    docs_install.py          # 把 docs/ 装进项目
  tests/
<项目>/shiftboss/             # 由 `shiftboss init` 装入的 docs/ 逐字节副本，只读
<项目>/shiftboss/plans/       # 任务书；归档在 plans/archive/
<项目>/shiftboss.toml         # 项目配置
<项目>.shiftboss/             # 家目录：hub.git、control/、执行方副本、verify/
```

1. 包内不出现任何项目专有路径、模块名或领域概念；项目的东西只经 `shiftboss.toml` 进来，项目专有约定写在项目文档里。
2. 包在项目里只占顶层 `shiftboss/` 与 `shiftboss.toml`，不假定项目有其他目录（如 `docs/`）。
3. 命令行只有一个入口 `shiftboss`，子命令：`inbox`、`taskbook`、`dispatch`、`merge`、`bell`、`recover`、`regen`、`verify`、`bootstrap`（`init` 同义）。
4. 执行方只读得到自己副本里的东西，所以通用规则要装进项目仓库；项目里的那份与包内源不一致时，`bootstrap --check` 与合并快速检查都会报出。
5. shiftboss 不设常驻验证：常驻验证验的是项目；shiftboss 改动不频繁，改后由派活方做语法检查与点名到单条测试方法的反证。

## 3. 可见性分区

| 区 | 内容 | 位置 |
| --- | --- | --- |
| 共享 | 任务书、通用流程规则、完成标准、任务书格式 | 仓库内 |
| 仅派活方 | 核对要点与执行方评估（现 `EXECUTORS.md`）、错题本、派活台账、配额、门铃文件、验证筛查与报告 | 控制目录 |
| 仅执行方 | 本班交代 | 该线任务书的轮次记录 |

1. 控制目录位置由 `shiftboss.toml` 指定，缺省为家目录下的 `control/`；工作副本内不存放、不链接控制目录。
2. 文件名去掉 `mm-` 前缀，按用途命名（`sessions.json`、`quota.json`、`inbox-acked.json`、`inbox-rejected.json`、`bell.log`、`bell.lock`、`mistakes.md`、`EXECUTORS.md`、`verification/`）。
3. post-receive hook 装入时写死门铃的绝对路径，因为 hook 不读配置；改控制目录后须重装 hook。
4. 工作副本是软沙盒：只以本地 hub 为 `origin`，hub 不设上游，执行方接触不到公开远端，免得弱执行方误推。
5. 硬沙盒（执行方以独立账户或沙箱运行）列为可选项，确有必要时再做。

## 4. 平台适配

1. 逻辑与状态一律用 Python：收件箱现算、任务书解析、合并检查、验证调度、门铃判定。
2. 与操作系统打交道且各平台做法不同的操作写成原生脚本，随包分发，由 `native.py` 调用：

| 操作 | Windows | POSIX |
| --- | --- | --- |
| 在新终端标签起一个会话 | `launch.ps1`（wt.exe，缺则新控制台） | `launch.sh`（tmux） |
| 后台起常驻进程 | `spawn.ps1` | `spawn.sh`（nohup／setsid） |

3. 契约：参数经临时文件传入（UTF-8，字段以 NUL 分隔），脚本在标准输出最后一行打印一行 JSON（`{"ok": true, ...}` 或 `{"ok": false, "error": "..."}`），退出码非零即失败；内容不经命令行字符串拼接。
4. 缺某平台脚本时该操作报「未支持」，不回退到 Python 模拟。
5. 杀进程树与列进程用 psutil：它本身跨平台，不另写脚本。

## 5. 执行方插件

1. 每个执行方一个模块，接口见 `workers/__init__.py`：进程名、能否投消息、起 TUI 的 argv、会话号、步数、停在等输入、模型、投消息、沙箱告警、副本准备。
2. 包内自带 `codex`、`agy`、`omp`；另装的执行方经 entry points 组 `shiftboss.workers` 注册。
3. 执行方特有的会话文件格式、安装位置与卡死特征只写在各自插件里；缺省执行方由 `[workers] default` 给。

## 6. 项目配置 `shiftboss.toml`

```toml
[project]
local_config = ".mmeditor-local.json"   # 不进 git 的本机配置文件名；缺省没有
control_dir  = "../MMEditor.shiftboss/control"   # 缺省为副本目录下的 control/
copies_dir   = "../MMEditor.shiftboss"     # 家目录：hub.git、control/、工作副本与验证工作区；缺省从 bare 位置认

[regen]
plan    = "regen.json"                  # 生成计划：步骤、闸、分层、后台验证策略
catalog = "project/derived-artifacts.json"   # 生产端产物清单

[workers]
default = "codex"                       # 任务书没写执行方时派给谁

[resources]
heavy_limit = 3                       # 整机同时跑的快速检查等重活；执行方会话数另由配额管

[merge]
js_module_prefixes = ["engine/"]        # 合并时按 ES 模块检查语法的路径前缀
assertion_dirs     = ["engine/tests"]   # 合并时列出断言增删的测试目录
```

1. 缺项时取中性缺省值，包内不假设任何项目路径；缺文件时 `dispatch` 拒派，由派活方从项目 README 提取、向用户补问后生成。
2. 生成计划结构复杂，放在 `plan` 指向的 JSON 文件里；`shiftboss.toml` 只放设置。

## 7. 迁移步骤

每一步完成后本项目照常派活、合并、验证，门铃与常驻验证按新命令重启；每步单独提交并向用户汇报。

1. 建 `shiftboss/` 包骨架与 `pyproject.toml`，以可编辑模式安装；`python -B -m workflow.tools.*` 不变。
2. 逐个把模块搬进 `src/shiftboss/`，原 `workflow/tools/*` 暂留为只转发的薄壳；命令行 `shiftboss <子命令>` 可用。
3. 引入 `shiftboss.toml`，各处写死的项目名与路径改由它给出；产物清单移出工具目录。
4. 控制目录落地：把 bare 仓库旁的 `mm-*` 文件迁入 `shiftboss/` 控制目录并去前缀；`EXECUTORS.md` 等仅派活方内容移入控制目录。
5. 执行方插件化与平台脚本化：拆出 `workers/` 与 `scripts/`，去掉派活逻辑里的执行方分支与 PowerShell 拼串。
6. 文档切换：通用文档进包内 `docs/` 并装入项目 `docs/shiftboss/`；CLAUDE.md、AGENTS.md、钩子改为 `shiftboss` 命令；删除 `workflow/`。
7. 包拆成独立仓库（与项目同级的 `shiftboss/`），项目以 `shiftboss init` 装载；再拿一个小的第二仓库试装，修掉暴露出的耦合；之后再定英文文档、版本号与发布。

## 8. 暂不做

1. 发布到 PyPI；第一版只在本机以可编辑模式安装。
2. 项目的对外发布：使用 shiftboss 的项目仓库在 git 历史里留下高频提交与协作细节（轮次交代、交回、派生物提交），不直接公开；发布时另开发布副本，在那里整理历史详略后推送到公开远端。工作仓库的 `origin` 始终是本地 hub，与公开远端互不相干。
