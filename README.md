# shiftboss

一个 AI 派活方带多个编程代理轮班干活的工作流工具，所有状态都建立在 git 上。

- **派活方**（通常是一个 Claude 会话）写任务书、派活、核对交回、合并。
- **执行方**（Codex 等编程代理）各在自己的工作副本和分支上干活，干完推送。
- 推送会**响门铃**，叫醒派活方来核对；合并后由**常驻验证**在独立副本里生成并跑测试，出了问题再响门铃。

设计细节见 [DESIGN.md](DESIGN.md)；派活方与执行方要遵守的流程规则见 [src/shiftboss/docs/README.md](src/shiftboss/docs/README.md)。

## 安装

需要 Python 3.11 以上。在本仓库里以可编辑模式安装：

```bash
pip install -e .
```

## 在项目里启用

这是唯一需要人手工执行的命令。在项目仓库根目录运行：

```bash
shiftboss init
```

它会做这些事：

- 把通用流程规则装进项目的 `shiftboss/` 目录（只读副本）；
- 在项目旁边建家目录 `<项目>.shiftboss/`，里面有本地中转仓库 `hub.git`、只给派活方看的 `control/`、执行方的工作副本，以及验证工作区；
- 读取项目根目录的 `shiftboss.toml`。各项配置的含义见 DESIGN.md 第 6 节。

`shiftboss` 的命令都要在项目工作树里运行，项目根目录按当前目录所在的工作树来认。

## 日常使用

以下命令都由派活方（AI）自己调用，人不需要手工执行；人只需要向派活方提需求、定优先级、验收结果。列出来是为了让人看懂派活方在做什么。

一件事从开始到收尾：

```bash
shiftboss taskbook new <名字> --kind task --copy codex1   # 建任务书
#   编辑 shiftboss/plans/<名字>/contract.md，写清要做什么、怎么算做到
shiftboss taskbook set <名字> --field 调度状态=待派        # 写完标成可派
shiftboss dispatch <名字> -m "按合同完成 2.1。"           # 派给执行方
shiftboss bell --once                                     # 等门铃：有人推送或验证出结果时返回
shiftboss inbox list                                      # 看哪些可以核对
shiftboss merge origin/codex/<名字>                       # 核对通过后合并
shiftboss merge --done <名字>                             # 收线归档
```

其他常用命令：

| 命令 | 用途 |
| --- | --- |
| `shiftboss inbox status` | 当前所有在办线、执行方会话与验证进度一览 |
| `shiftboss dispatch <名字> --rework` | 打回后让执行方返工 |
| `shiftboss verify claim <票据> <任务书>` | 把验证报出的失败关联到修复线 |
| `shiftboss recover` | 执行方进程意外退出、常驻验证留下旧锁时把它们接回来 |
| `shiftboss regen` | 按项目的生成计划重新生成派生物并跑一遍闸（平时由常驻验证跑） |
| `shiftboss bootstrap --check` | 只读核验家目录、工作副本与项目里的流程规则副本，缺项就报告 |

各子命令的完整参数用 `shiftboss <子命令> --help` 查看。

## 目录

采用 Python 打包指南推荐的 src 布局：包源码放在 `src/` 下，未安装时不会误导入仓库里的源码。

```text
src/shiftboss/      包源码：命令、执行方插件、平台脚本、钩子
src/shiftboss/docs/ 通用流程规则与任务书模板（装进项目的源）
tests/              测试
DESIGN.md           设计
```
