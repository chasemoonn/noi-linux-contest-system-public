# V1 执行基线

记录时间：2026-08-11（Asia/Shanghai）

## 1. 隔离开发位置

- 工作树：`work/noi-linux-contest-system-v1`
- 分支：`feature/noi-v1-product-contract`
- 基础提交：`6e5ae8ba49fcd01614d7e1f628be80bc616b052e`
- 基础树：`5a8ad4ba0464862c202133b20131633c3af4e7aa`

暂停发布的 `work/noi-linux-contest-system-public-snapshot` 保持不变。其提交 `36f8f3c8592f53934ed072ca285368128be3048e` 与上述基础提交解析到同一个 Git tree，因此新工作树没有丢失公开快照中的功能。

## 2. 已识别源码来源

| 路径 | 用途 | V1 处理 |
| --- | --- | --- |
| `work/noi-linux-contest-system-v1` | V1 独立开发工作树 | 唯一编辑目标 |
| `work/github-publish-noi-linux` | 原开发历史，HEAD 为 `6e5ae8b` | 保留，只作为历史来源 |
| `work/noi-linux-contest-system-public-snapshot` | 暂停发布的干净公开快照 | 保持不变 |
| `work/review_contract_base` | 早期审查基线 | 只读参考 |
| `work/noi-candidate-*` | 历史候选包 | 只读参考 |
| `work/xuanwu-*.sh/.py` | 生产应急、迁移和验收工件 | 不并入正式主流程 |

## 3. 现有能力

基础代码已经包含：

- OJ 比赛、名单、题目和提交通道；
- CSP PDF 和 AI 材料工作流；
- 可信 validator/oracle 边界；
- 一人一容器座位池；
- 阿里云生命周期和入口规则；
- 实时送评持久队列与幂等键；
- 目录截止冻结和收卷；
- 故障座位替换；
- OI 盲测；
- 老师后台和学生回收页；
- 部署、验证、公开发布检查和文档。

## 4. 与 V1 契约冲突的旧设计

以下旧能力不能原样保留：

1. 老师可选择 `web/folder/both`，存在多个正式代码来源；V1 只允许答案目录为唯一来源。
2. 开赛后新增学生需要老师显式批准；V1 以 OJ 报名为批准并自动扩座。
3. 座位池使用人工登记的正式上限与备用数；V1 默认按报名人数和 10%（至少 2 个）备用自动收敛。
4. 学生网页可粘贴或上传一份独立源码；V1 的所有输入必须先落入正式 `.cpp`。
5. 旧 `both` 模式可能优先网页版本而不比较截止目录；V1 必须比较截止快照与最后已确认版本。
6. 旧老师端是工程操作台；V1 必须收敛为五个阶段化页面和单一主要操作。
7. 旧时间契约以登记快照和重新登记为主；V1 要持续跟随 OJ 时间并公开同步状态。

## 5. 本地测试基线

使用源码基础树执行：

- Python `compileall`：通过；
- Hydro 插件 Node.js 测试：41/41 通过；
- Python unittest：发现 317 项，Windows 本地为 10 failures、7 errors、5 skipped；
- 17 个非通过项均集中在测试进入 Linux 专用 `deployment_lock` 前后的路径，不能作为 Linux CI 回归结论。

仓库 CI 的正式运行环境是 Ubuntu、Python 3.12、Node.js 22，并执行：

```text
python -m compileall -q orchestrator
python -m unittest discover -s orchestrator/tests -v
node --check hydro-plugin-orchestrator/index.js
node --test hydro-plugin-orchestrator/tests/*.test.js
python scripts/run_v1_fault_injection.py --require-linux
bash -n deploy/*.sh scripts/*.sh
python scripts/build_demo.py --check
python scripts/check_public_release.py
```

当前 Windows 主机没有可用 Docker，WSL 访问被拒绝。因此 V1 的 Linux/POSIX、Docker、镜像和容量证据必须来自后续隔离 Linux CI 或独立 Linux 测试机，不能用 Windows 结果冒充。

故障注入门是独立的候选硬门，覆盖：OJ 已创建记录但响应丢失、Hydro 插件重启、
控制器重启、只读核对时网络中断，以及两个 SQLite 连接并发认领同一不确定递交。
本机可运行同一逻辑门进行快速反馈；正式 CI 必须带 `--require-linux` 在 Ubuntu 上再次通过。

## 6. 生产安全基线

在任何远端变更前必须重新只读确认：

- 普通 OJ 首页、登录、健康和评测正常；
- 没有活动中的 NOI Linux 比赛；
- 原 NOI 控制器保持停止或入口关闭；
- 比赛云资源和学生公网入口关闭；
- 当前 OJ、Caddy、PM2、数据库和插件状态已备份；
- 所有部署文件来自已签名/已哈希的 V1 发布工件；
- 回滚命令与恢复材料已经在独立环境验证。

本文件不把历史现场快照当作当前确认。远端状态会漂移，生产启用前必须重新读取。

## 7. 阶段0结论

- 现有源码适合作为 V1 基础，不需要从零重写；
- 产品状态、座位同步、答案来源和 UI 需要实质重构；
- 公开快照和历史应急工件必须保持隔离；
- Windows 本地可承担纯 Python、Node 和文档开发，发布仍必须经过 Linux CI、桌面金丝雀和 15+2 容量验收。
