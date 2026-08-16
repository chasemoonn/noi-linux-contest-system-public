# V1 测试部署、提交与回滚计划

这份计划只适用于已通过 `verify_v1_candidate.py` 的候选。默认目标是**独立测试环境**；没有生产资格报告时禁止在老师正在使用的普通 OJ 上试装。

## 0. 角色与停止条件

- 执行者：只能运行已冻结候选中的脚本。
- 复核者：独立核对候选 SHA、组件 digest、现场作用域和回滚材料。
- 普通 OJ 观察者：只读观察首页、登录、准备服务、Hydro/PM2/Caddy/Mongo 状态。
- 任一门失败：停止，不用手改配置“凑通过”。

## 1. 只读预检

1. 在可信工作站核对候选目录及 manifest；需要正式部署时加 `--require-production-qualified`。
2. 在 Linux 测试机重新核对归档 SHA，并只解到 root 专用 staging。
3. 确认 Git revision、控制镜像 digest、桌面 image ID、插件 SHA 与资格报告完全相同。
4. 确认没有活动 OJ 比赛、准备任务、递交队列、通知队列、座位或未完成收卷。
5. 保存普通 OJ 的首页、登录、准备健康、四个 PM2 定义/PID/restart、Caddy 磁盘与活动配置、Mongo 健康基线。
6. 确认比赛 ECS 为 `Stopped`，学生临时规则为 0，静态管理规则精确，固定 EIP 与目标实例一致。
7. 确认部署锁和镜像发布锁为 root 管理的普通单链接文件；存在未完成事务 marker 时先恢复，禁止删除 marker 后继续。

## 2. 备份与事务准备

首次写入前必须在 root-only 目录保存并 fsync：

- 当前源码 release 快照与 revision；
- 编排器镜像 ID、Compose 展开配置和容器定义；
- Hydro addon、插件 env 和四个幂等状态文件；
- Caddyfile、考试片段和活动 JSON；
- PM2 dump、Hydro 定义和全部 `ORCHESTRATOR_*` 环境映射；
- 运行配置、数据库及 WAL/SHM 一致快照；
- 云资源、EIP、安全组规则的只读快照。

写入 durable pending marker 后才允许第一项服务变更。marker 包含候选 revision、备份路径和每个备份 SHA，不含秘密。
备份必须生成 `v1-install-backup-manifest.schema.json` 规定的完整清单，并由
`scripts/verify_v1_install_backup.py --expected-plan-id <ID>` 在首个服务写入前通过；WAL/SHM、
PM2 backup 即使不存在也要以 `present=false` 明确记录，不能漏字段。备份目录不得含未入清单的文件。

## 3. 最小提交顺序

服务组合必须由 `orchestrator/services/install_transaction.py` 的固定阶段状态机协调。它在调用每个
可能写入的驱动前先持久化 `in_progress`；进程异常退出后只能先回滚该不确定阶段，再逆序回滚已
完成阶段，绝不从旧 journal 继续向前。已有站点升级的真实驱动已接入该状态机，
`service_apply_coordinator=passed`；首次干净安装的六阶段驱动和恢复器也已接入，但真实 Linux
成功/失败/断电矩阵与其余资格证据仍是独立硬门。首次安装不得复用要求旧控制器存在的升级备份，见
[`docs/V1_CLEAN_INSTALL_TRANSACTION.md`](../docs/V1_CLEAN_INSTALL_TRANSACTION.md)。
13 场景的快照纪律、命令顺序和证据组合见
[`deploy/V1_CLEAN_INSTALL_REHEARSAL.md`](V1_CLEAN_INSTALL_REHEARSAL.md)。

1. 安装候选源码到新的 root-only release 目录，不覆盖旧 release。
   该步骤必须只使用 `scripts/stage_v1_source_release.py`：先 `--plan`，人工核对 plan ID，
   再以相同候选、外部 manifest SHA 和 plan ID 执行 `--apply`。它只原子建立
   `source-releases/<revision>-<manifest-prefix>` 与 `current-source`，不运行任何服务脚本；
   出现 pending marker 时只能带原 plan ID 执行 `--recover`，不能手工删除。
2. 导入并核验精确桌面镜像，不移动正式 tag。
3. 部署控制镜像候选，仅运行静态 doctor 和本机健康，不公开学生入口。
4. 安装 Hydro 插件并核验本机接口；公网接口继续固定 404。
5. 热加载 Caddy 的关闭片段，确认 `/s/*` 为 503、普通 OJ 路由和 Caddy PID不变。
   两个 Caddy 子脚本只能用 `NO_CADDY_LOAD=1` 改写同一个 root-only candidate；随后
   `scripts/commit_v1_caddy_config.py` 用 `/adapt` 得到原生 JSON，GET `/config/` 绑定基线与
   ETag，并只对 `/config/` 发一次 `If-Match` 条件 POST。HTTP 412 或磁盘/active 漂移直接回滚，
   不得重试旧 candidate，也不得把 `If-Match` 加到 `/load`。
6. 用一场隐藏测试赛执行单座验收；通过后才允许创建其余座位。
7. 单座、故障恢复和 15+2 全部通过后，才生成 `production_qualified=true` 的报告与候选。
8. 最终提交时原子切换 release 指针；写入并 fsync committed marker 后才清 pending marker。

## 4. 回滚顺序

回滚目标是恢复部署前普通 OJ 和关闭的 NOI Linux，不是强行保留一场失败测试赛。

1. 撤销学生入口并证明 `/s/*` 为 503；撤销自管学生安全组规则。
2. 停止候选控制器和专用比赛 ECS，等待云状态为 `Stopped`。
3. 删除候选座位和测试赛网关；保留故障证据包。
4. 恢复旧控制器 release/config/镜像定义。
5. 精确恢复 Hydro addon、PM2 live 定义和持久 dump；验证环境映射与备份一致。
6. 使用当前 ETag/活动配置保护恢复 Caddy，防止覆盖无关并发更改。
7. 逐项比较数据库、Caddy、PM2、插件、云和普通 OJ 基线。
8. 只有本地恢复和云关闭都验证成功，才写 rollback verified 并清 pending marker。
   `rollback verified` 必须由 `scripts/verify_v1_install_rollback.py` 对恢复后重新采集的同一
   21 槽位快照逐项比较后生成；不得依据 shell 返回码或人工勾选生成。receipt 只能在 pending
   marker 已清、普通 OJ 规范化快照与基线完全一致时原子落盘。

如果回滚无法验证，保持学生入口关闭、ECS 停止和候选控制器停止，标记人工处置；绝不能自动重试提交。

## 5. 明确禁止

- 在有活动普通 OJ 比赛时安装或重启 Hydro/Caddy/Mongo；
- 从可由普通账号写入的路径直接以 root 执行脚本；
- 使用 `latest` 或只有 tag、没有 digest 的镜像；
- 跳过单座直接跑 15+2；
- 把“页面 200”当成收卷、送评或停机成功；
- 把旧版本、旧实例规格或另一台机器的容量证据沿用到新候选。
