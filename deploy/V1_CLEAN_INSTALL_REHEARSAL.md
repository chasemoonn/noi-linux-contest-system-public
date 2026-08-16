# V1 首次干净安装 13 场景资格演练

本文是首次干净安装资格矩阵的唯一执行顺序。它只适用于可随时销毁和恢复快照的
Linux/Docker 资格实验室，禁止在生产 OJ、教师正在使用的 OJ 或生产云资源上运行。

完成本文只证明“首次安装事务在成功、阶段失败和进程被杀后可收敛”。它不替代 Linux CI、
跨机器镜像导入回滚、独立教师安装、单座、六类故障恢复或 15+2 一小时容量验收。

## 1. 固定对象与停止条件

开始前固定以下对象，并通过独立可信通道保存其 SHA256：

- 候选目录及 `release-manifest.json`；
- 候选 manifest 的外部 SHA256；
- `private-clean-install-plan.json` 及生成器输出的 SHA256；
- clean baseline backup manifest 的 SHA256；
- 控制器 image digest、桌面 image ID、桌面源码 revision、Hydro 插件摘要；
- 一个形如 `NOI-V1-QUAL-...` 的本次资格 marker。

任一字节、路径、image ID、普通 OJ 基线、云关闭态或目标快照发生变化，都必须废弃本轮全部
场景并重新生成基线，不能只补跑失败场景。私有计划必须是 `scope=qualification-lab`；生产计划
会被场景驱动器拒绝。

硬停止条件：

- 主机不是独立 Linux 资格机，或执行者不是 root；
- Git checkout、候选、计划、备份、证据目录或其祖先可被非 root 替换；
- 普通 OJ 有活动比赛、队列、异常进程或待收卷；
- 已存在 NOI 控制器、插件、配置、数据库、源码指针、学生规则或座位；
- Caddy 磁盘与活动配置不等价，考试域名/8600/import 已存在；
- 云主机不是 `STOPPED`，或将使用生产实例、生产安全组、生产域名；
- 任一事务 pending marker 来源不明。

## 2. 建立一次可重复的 clean baseline

1. 在一台全新资格 VM 上安装与目标相同的 Ubuntu、Docker、PM2/Hydro/Caddy 版本；普通 OJ
   使用合成数据和资格域名，不复制学生数据或生产秘密。
2. 将可信 checkout、候选和桌面离线镜像放入 root-only 固定路径。所有场景中绝对路径必须相同。
3. 用 `build_v1_clean_install_backup.py` 采集 clean baseline，并用
   `build_v1_clean_install_backup_manifest.py` 封存；二者都成功后再继续。
4. 用 `prepare_v1_clean_install_materials.py --apply` 原子建立本轮固定私有材料。
5. 用 `build_v1_private_clean_install_plan.py --qualification-lab` 生成私有计划。把生成器标准输出中的
   `private_plan_sha256` 从终端独立复制到 root-only 运行记录，禁止从计划文件内部自报值。
6. 运行一次只读 plan load/backup verifier，并确认现场仍是 clean target。
7. 关机并建立只读母快照 `clean-baseline`。母快照之后不得启动或修改。

站点路径和秘密引用因实验室而异，所以本文不提供可盲目粘贴的真实参数。各生成器的完整参数必须
从可信 checkout 的 `--help` 取得；不得用人工 JSON 替代生成器。生成完成后应固定：

```bash
PLAN=/root/noi-v1/private-<plan-id>/private-clean-install-plan.json
PLAN_SHA=<生成器输出的64位小写SHA256>
OBS=/root/noi-v1/evidence/clean-install-cases
PHASES='source_release clean_materials hydro_integration closed_frontend controller post_install_verification'
```

`PLAN`、候选、backup、Docker socket、Caddyfile、配置和 token 的绝对路径已经写入计划；恢复快照后
不得移动这些文件。证据目录必须位于独立 root-only 证据盘，或在每个场景封存后立即以只读方式
导出，再恢复母快照。

## 3. 运行精确 13 个独立场景

每个场景都从 `clean-baseline` 的新克隆开始。不得在一个已经成功安装或已回滚的克隆上连续跑
第二个场景。启动克隆后先重验计划 SHA、clean baseline、普通 OJ 和云关闭态；然后只运行一个
case runner。

完整成功：

```bash
sudo python3 scripts/run_v1_clean_install_rehearsal_case.py \
  --private-plan "$PLAN" --expected-plan-sha256 "$PLAN_SHA" \
  --output-directory "$OBS/success" --kind success
```

六个可控阶段失败（每个循环项必须在一个新的母快照克隆中运行）：

```bash
for phase in $PHASES; do
  sudo python3 scripts/run_v1_clean_install_rehearsal_case.py \
    --private-plan "$PLAN" --expected-plan-sha256 "$PLAN_SHA" \
    --output-directory "$OBS/phase_failure-$phase" \
    --kind phase_failure --phase "$phase"
done
```

六个进程被杀/断电边界（同样每项使用新的母快照克隆）：

```bash
for phase in $PHASES; do
  sudo python3 scripts/run_v1_clean_install_rehearsal_case.py \
    --private-plan "$PLAN" --expected-plan-sha256 "$PLAN_SHA" \
    --output-directory "$OBS/power_loss-$phase" \
    --kind power_loss --phase "$phase"
done
```

上面两个循环只是命令模板，不允许在同一未恢复 VM 中直接一次跑完整循环。实验室调度器必须在
每次迭代前销毁克隆、从母快照创建新克隆、挂载同一证据盘并重验 plan SHA。

case runner 会自己完成以下动作，操作者不得拆开模拟：

- 在新进程组运行事务，并为成功或失败封存执行日志；
- 普通失败只在目标阶段 receipt 和 journal 已 fsync 后注入；
- power-loss 由父监督器确认 ready marker/PID/阶段后发送 `SIGKILL`；
- 对失败进程执行同计划、同 SHA 的 resume，并要求 `rollback_verified`；
- 再次运行 live 成功态或 clean rollback 验证；
- 封存普通 OJ 前后快照、终态回执和所有日志 SHA。

若 case runner 返回非零，该目录不属于合格证据。不得改 observation、删 marker、手工恢复后伪装成
通过。只有“事务已到正确终态、但最后一次只读外部探针临时失败”时，才可在同一未变快照上按错误
提示重跑 observation collector；不能再次执行安装事务。

## 4. 场景目录验收

组合前，证据根必须恰好只有以下 13 个目录：

```text
success
phase_failure-source_release
phase_failure-clean_materials
phase_failure-hydro_integration
phase_failure-closed_frontend
phase_failure-controller
phase_failure-post_install_verification
power_loss-source_release
power_loss-clean_materials
power_loss-hydro_integration
power_loss-closed_frontend
power_loss-controller
power_loss-post_install_verification
```

禁止加入 `.bak`、人工说明、临时日志或第二次尝试目录。每个目录的固定文件集合、mode、owner、大小和
SHA 由 observation 校验器决定；失败/断电场景必须证明普通 OJ 前后规范化字节完全相同，成功场景
允许且只允许计划内 Hydro 重启，并封存前后身份。

## 5. 采集组件身份并构建矩阵

组件事实必须由真实角色采集器生成，输出父目录为 root-owned 0700，文件为 0600：

```bash
sudo python3 scripts/collect_v1_components.py --role control \
  --docker-bin /usr/bin/docker --container noi-orchestrator \
  --output /root/noi-v1/evidence/components/control.json

sudo python3 scripts/collect_v1_components.py --role desktop \
  --docker-bin /usr/bin/docker --container <资格座位容器名> \
  --output /root/noi-v1/evidence/components/desktop.json

sudo python3 scripts/collect_v1_components.py --role oj \
  --plugin-root <已安装且精确两文件的Hydro插件目录> \
  --output /root/noi-v1/evidence/components/oj.json
```

desktop 事实可以在后续单座资格机上采集，但必须来自同一候选绑定的真实运行座位，不能用 image tag
或人工抄写替代 image ID/labels。将三个事实以保字节方式送到矩阵复核机，再运行：

```bash
sudo python3 scripts/build_v1_clean_install_rehearsal_matrix.py \
  --private-plan "$PLAN" --expected-plan-sha256 "$PLAN_SHA" \
  --observations-root "$OBS" \
  --control-component /root/noi-v1/evidence/components/control.json \
  --desktop-component /root/noi-v1/evidence/components/desktop.json \
  --oj-component /root/noi-v1/evidence/components/oj.json \
  --qualification-marker 'NOI-V1-QUAL-<本轮随机标识>' \
  --output /root/noi-v1/evidence/v1-clean-install-rehearsal-matrix.json

python3 scripts/verify_v1_clean_install_rehearsal.py \
  /root/noi-v1/evidence/v1-clean-install-rehearsal-matrix.json
sha256sum /root/noi-v1/evidence/v1-clean-install-rehearsal-matrix.json
```

矩阵 builder 要求场景集合精确、同一 plan/backup/candidate、真实组件绑定、Linux root 和安全祖先；
它会生成随机 session ID 与匿名主机 ID。矩阵及 13 个原始目录必须一起保留，不能只保留组合 JSON。

## 6. 通过与后续顺序

本门只有同时满足以下条件才通过：

- `verify_v1_clean_install_rehearsal.py` 退出 0；
- 成功场景为 `committed`；
- 12 个失败场景全为 `rollback_verified`；
- 六阶段的普通失败与 power-loss 各恰好一项；
- 所有 rollback 普通 OJ 前后字节一致，clean baseline 全恢复，云保持关闭；
- 矩阵 source/components/plan 与本轮候选完全一致。

随后才能把矩阵交给另一台机器上的独立老师。老师必须为自己的干净快照另建私有计划，只运行
`phase_failure-post_install_verification`，再用
`collect_v1_independent_teacher_install_observation.py` 自动密封安装与回滚事实；不能复用本矩阵计划，
也不能手写 observation。完整命令见 `V1_QUALIFICATION.md` 的“独立老师安装证据”。仍须依次完成：
跨机器镜像导入回滚、独立老师从候选安装与回滚、单座、六类故障和固定 15+2 一小时长稳。任一后续门改变源码、镜像、插件、实例规格
或网络画像，都使相关旧证据失效，必须重新演练。
