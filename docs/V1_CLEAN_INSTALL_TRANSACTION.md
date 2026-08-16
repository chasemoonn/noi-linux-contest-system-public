# V1 干净目标首次安装事务

本文只定义普通 OJ 已健康运行、但从未安装 NOI Linux 组件的首次安装。它与已有站点升级是两种不同事务，不得伪造一个“旧控制器”来复用升级路径。

## 干净目标定义

备份采集前必须同时证明：

- 安装根、`current-source`、编排器配置、私有 env 和 SQLite 数据库不存在；
- `noi-orchestrator` 容器不存在；
- Hydro 的 NOI addon 目录、插件 env、token 和幂等状态目录不存在；
- Caddyfile 不含考试域名、8600 上游、NOI import 或 Hydro 私有路由加固标记，且磁盘配置与活动 JSON 完全等价；
- 普通 OJ 首页、登录、Prep 和四个 PM2 定义健康；
- 云主机为 `STOPPED`，学生规则为 0，关闭态语义经受信的只读云探针采集。

`scripts/build_v1_clean_install_backup.py` 仅采集上述基线，不做服务写入。它产生
`v1-clean-install-backup-manifest.schema.json` 清单；清单将“不存在”作为显式、可哈希、可复核的状态，而不是省略字段。

## 预定提交顺序

1. 核验生产候选和独立的 manifest SHA。
2. 采集并封存 clean baseline；运行 `verify_v1_clean_install_backup.py`。
3. 生成 root-only 私有安装计划，绑定候选、目标镜像、站点配置、共享 token 、云快照和 clean baseline SHA。
4. 安装不执行脚本的冻结源码 release。
5. 原子建立私有配置、token、空数据库和项目目录；再安装 Hydro 插件。
6. 只发布关闭的 Caddy 前端，对外 `/s/*` 必须为 503。
7. 创建唯一、digest 固定的控制器，它必须健康、无活动座位、云关闭。
8. 完整终验后才写 committed；任何不确定响应只能进入持久回滚。

私有输入由 `scripts/build_v1_private_clean_install_plan.py` 在目标 Linux 主机的 root-only
目录中组装。生成器把公开 `plan_id`、候选 manifest、clean backup SHA、资格镜像 digest、
站点配置、站点 env、Hydro 插件 env/token、Caddyfile、固定 Docker socket 及控制器创建模板
绑定为一个 `private-clean-install-plan.json`。模板不是任意 Docker 配置：只允许 host 网络、
只读根文件系统、drop-all capabilities、no-new-privileges、固定 `/tmp` tmpfs，以及 7 个精确
挂载目标；任何额外挂载、环境变量或 Docker 控制 socket 都会拒绝。

执行入口与升级共用同一条管理员命令：

```bash
python3 scripts/noictl.py install --apply \
  --private-plan /root/noi-v1/private-<plan_id>/private-clean-install-plan.json \
  --expected-plan-sha256 <从生成器标准输出独立复制的64位SHA256> --json
```

`noictl` 先用单文件描述符重验私有计划摘要和 `operation`，然后只把 `clean-install`
分派给 `apply_v1_clean_install.py`。执行器不会读取另外的 `--config`，也不会接受候选路径、
镜像或资格开关覆盖计划。

## 回滚语义

首次安装的 `rollback_verified` 必须额外证明：新控制器已删除；配置、env、数据库、token、插件树、状态目录、Caddy 片段和源码指针恢复为不存在；Caddy 磁盘/活动配置、Hydro/PM2 与普通 OJ 恢复到备份基线。可保留未被 `current-source` 引用的 root-only 签名源码证据，但不能留下任何已安装服务、活动入口或自动启动项。

## 当前交付边界

干净基线采集、封存、语义核验、私有计划生成器、六阶段组合执行器、终态恢复验证和
`noictl` 分派均已进入源码门。事务在 live rollback 验证后先持久写入独立验证收据；事务框架
支持在该收据之后恢复可选的证据清理。当前 clean-install 不执行破坏性的源码清理：冻结 release
会作为 root-only、未被 `current-source` 引用、无活动入口的签名恢复证据保留，以保证掉电后仍有
唯一可信恢复程序；它不属于已安装服务或自动启动组件。

尚未通过的上线硬门仍包括：隔离 Linux/Docker 的首次安装成功/失败/断电矩阵、跨机器镜像
导入回滚、独立教师按文档安装、单座真人 canary、故障恢复，以及 15+2 座持续 60 分钟容量
验收。在这些证据进入正式资格报告前，不得在正在使用的 OJ 上执行首次安装。

首次安装矩阵不是一条“跑过”的日志。`v1-clean-install-rehearsal-matrix.schema.json` 要求同一
源码和组件完成 13 个彼此独立的隔离快照场景：一次完整 committed，以及对
`source_release`、`clean_materials`、`hydro_integration`、`closed_frontend`、`controller`、
`post_install_verification` 六阶段分别注入普通失败和进程被杀/断电。12 个回滚场景都必须用
同一计划恢复到 `rollback_verified`，且 clean target、Caddy、Hydro、云关闭态、普通 OJ
稳定进程和健康状态全部复原。Hydro 集成阶段允许安装事务中唯一计划内的 Hydro 重启，因此
普通 OJ 前后快照不要求逐字节相同；比较器仍强制 Caddy、Hydro sandbox、MongoDB 的 PID、
重启计数和在线状态逐项不变，并要求首页、登录页、Prep 与数据库健康。两个快照分别以 SHA256
封存，矩阵原始字节会被独立教师签名证据固定；缺项、重复场景或只靠人工描述均不能通过。

隔离主机只能用 `scripts/run_v1_clean_install_rehearsal_scenario.py` 驱动这些边界。普通失败模式
在目标阶段的 apply 收据和 pending journal 已 `fsync` 后抛出资格异常；power-loss 子进程先原子
写入 root-only ready marker，再向自身发送 `SIGKILL`。监督进程确认子进程确实被杀后，必须用
原私有计划和原 SHA 运行 `--mode resume`，不得生成新计划掩盖未完成事务。该入口硬性拒绝
`production` 私有计划，且不由 `noictl install --apply` 暴露给生产执行路径。

断电场景不得人工分两条命令拼接。`scripts/run_v1_clean_install_power_loss_supervisor.py` 必须作为
父进程启动场景子进程，验证 root-only ready marker 与子 PID、计划和阶段完全一致，要求退出原因
精确为 `SIGKILL`，清理该独立进程组，然后以同一私有计划字节和 SHA 运行恢复。恢复标准输出只能
是一条 `rollback_verified` JSON；子日志、恢复日志和 ready marker 都以 SHA256 进入后续机器证据。

每个隔离快照结束后，必须由 `collect_v1_clean_install_rehearsal_observation.py` 再次执行 live
成功态或 clean rollback 只读验证，并封存 stable ordinary-OJ 前后快照、clean backup manifest、
执行日志和事务终态收据。13 个场景目录只能命名为 `success`、六个
`phase_failure-<phase>` 和六个 `power_loss-<phase>`，目录内文件集合和每个 SHA 都是固定契约。
最后只能用 `build_v1_clean_install_rehearsal_matrix.py` 聚合：它重验每个封存目录、共同的逻辑
plan ID、候选 revision/tree，以及由真实 control/desktop/OJ 角色采集器生成的组件身份。每个独立
快照因启动后的 PM2 PID 不同，必须生成并逐场封存自己的私有计划 SHA 和 clean baseline SHA；
矩阵要求该 baseline SHA 与同场 backup manifest 精确相等。矩阵因此不接受人工填写的布尔验收表。

每次恢复隔离快照后，操作者只运行 `run_v1_clean_install_rehearsal_case.py`：它创建新的 0700
场景目录，成功/普通失败使用受控子进程，断电使用上述监督器，最后直接调用 observation 采集器。
若执行已达到终态但封存因只读网络探测失败，可在同一快照对该目录单独重跑 observation 采集器；
不得重新执行事务或覆盖原日志。完成 13 个目录后再运行 matrix builder，任何缺项均保持 NO-GO。
