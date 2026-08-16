# V1 Linux 与 15+2 资格验收

资格报告使用 `release/v1-qualification-report.schema.json`。所有引用文件必须保存在私有证据目录，清单只保存相对引用和 SHA256，不保存密码、token、Cookie、AccessKey、学生源码或桌面口令。

资格报告必须由 `scripts/build_v1_qualification_report.py` 从已验证的组合证据生成，禁止复制
示例后手工把状态或故障布尔值改成通过。签名证据协议、只读生产安装计划、已有站点升级和首次
干净安装的事务化 `install --apply` 均已进入源码门。没有完成真实独立 Linux 安装、六类故障恢复
与 15+2 演练时，对应事实
必须保持 `pending`，候选只能进入隔离资格测试机，不能宣称生产合格。

任何时点都可以用只读差距报告核对剩余门槛；它不会连接服务器，也不会把示例或人工声明当成通过：

```bash
python3 scripts/report_v1_launch_readiness.py qualification-report.json
```

输出固定列出七项资格门及 `passed/pending/failed` 计数。只有七项全为 `passed` 且报告本身为
`production_qualified=true` 时，生产安装计划才开放。机器可读 readiness 还单独列出软件交付门：
源码 release 事务、完整备份、服务 apply 协调器和精确回滚验证已经实现；Linux 首次安装
成功/失败/断电 13 场景仍必须产生真实矩阵证据。只有资格七门和软件交付五门全部通过时，
`production_install_apply_available` 才能为 true。

首次干净安装矩阵的唯一操作顺序见
[`V1_CLEAN_INSTALL_REHEARSAL.md`](V1_CLEAN_INSTALL_REHEARSAL.md)。每个场景必须从同一 clean
母快照的新克隆开始；不得在生产 OJ 上演练，也不得把一个 VM 上连续执行的 13 条命令当成矩阵。

## A. Linux CI

- Ubuntu 22.04/24.04 上 Python 编译与全部单元测试通过；
- Node 22 上插件语法和全部测试通过；
- 所有 shell 脚本 `bash -n` 通过；
- demo、V1 产品合同、公开发布边界检查通过；
- 记录 runner、commit、开始/结束时间和完整日志 SHA。

统一入口为：

```bash
sudo "$(command -v python)" scripts/run_v1_linux_ci.py --output v1-linux-ci-evidence.json \
  --log-directory v1-linux-ci-logs
sudo "$(command -v python)" scripts/verify_v1_linux_ci_evidence.py \
  v1-linux-ci-evidence.json --log-directory v1-linux-ci-logs \
  --expected-revision "$(git rev-parse HEAD)"
sha256sum v1-linux-ci-evidence.json
```

生成器拒绝非 Linux、非 root 和脏工作树，并在安全的 root-only 临时目录内运行全部子门，
把 revision、tree、Linux root 身份及十项门的结果写入
JSON，并把每项 stdout/stderr 写入日志目录；独立核验器要求十项名称、顺序、状态及日志
SHA256 完全一致。GitHub Actions 会上传精确 revision
命名的 artifact。资格报告 `evidence.linux_ci.reference` 应引用下载后保存到私有证据目录的
该 JSON，相应 `evidence_sha256` 必须是原始 JSON 文件摘要，而不是 GitHub artifact ZIP 摘要。

## B. 跨机导入与回滚

这项资格必须使用两台不同的 Linux 主机。事实采集器只做只读核验，不代替导入、提升或
回滚。两台主机使用同一个随机 `session_id`，但证据只保存加盐后的匿名主机 ID，不保存
`/etc/machine-id` 原文。开始前要求：Git 工作树干净、没有 `noi.contest` 座位容器、目标
机没有 pending 镜像事务，并且正式镜像与 `current-image-source` 已形成一组可回滚基线。
collector 必须以 root 运行，精确 checkout、bundle、release manifest 父目录和证据输出目录
都必须由 root 管理且不可被组/其他用户写入；不要从下载目录或普通用户可换包目录直接采集。
安装阶段还必须预建共享锁；资格脚本不会在世界可写的锁目录中临时创建它：

```bash
sudo install -o root -g root -m 0600 /dev/null \
  /var/lock/noi-official-image-deploy.lock
```

```bash
session_id="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
revision="$(git rev-parse HEAD)"
```

在构建/导出主机完成镜像构建和离线包导出后，记录第一份事实：

```bash
python3 scripts/collect_v1_image_host_fact.py \
  --phase export --session-id "${session_id}" \
  --bundle-dir /root/noi-image-bundle \
  --release-manifest /root/noi-release/release-manifest.json \
  --output /root/noi-evidence/01-export.json
```

通过独立传输通道把完整 bundle、公开 release manifest、精确 Git checkout 和
`01-export.json` 送到第二台主机。先运行可信 checkout 中的导入器：

```bash
bash scripts/import-local-image-bundle.sh \
  --bundle-dir /root/noi-image-bundle \
  --release-manifest /root/noi-release/release-manifest.json
python3 scripts/collect_v1_image_host_fact.py \
  --phase imported --session-id "${session_id}" \
  --bundle-dir /root/noi-image-bundle \
  --release-manifest /root/noi-release/release-manifest.json \
  --app-root /opt/noi-linux-contest-system \
  --output /root/noi-evidence/02-imported.json
```

从 release manifest 读取并人工核对固定 `image_tag` 与 `image_id`，再使用受控事务提升**已经
导入的不可变镜像**；该入口不重新构建镜像：

```bash
sudo bash deploy/promote-imported-contest-image-local.sh \
  --image '<release manifest components.desktop.image_tag>' \
  --expected-image-id '<release manifest components.desktop.image_id>' \
  --source-root "$(pwd -P)" --source-revision "${revision}"
python3 scripts/collect_v1_image_host_fact.py \
  --phase promoted --session-id "${session_id}" \
  --bundle-dir /root/noi-image-bundle \
  --release-manifest /root/noi-release/release-manifest.json \
  --app-root /opt/noi-linux-contest-system \
  --output /root/noi-evidence/03-promoted.json
```

随后严格执行“回滚—再次提升同一不可变 image ID—最终恢复原基线”，每一步完成后立即采集
事实；中途出现错误或 pending marker 时停止，不得手工改 Docker 标签或删除 marker：

```bash
sudo bash /opt/noi-linux-contest-system/current-image-source/deploy/rollback-contest-image-local.sh
# collect phase=rolled_back -> 04-rolled-back.json

sudo bash deploy/promote-imported-contest-image-local.sh \
  --image '<same fixed image_tag>' --expected-image-id '<same fixed image_id>' \
  --source-root "$(pwd -P)" --source-revision "${revision}"
# collect phase=repromoted -> 05-repromoted.json

sudo bash /opt/noi-linux-contest-system/current-image-source/deploy/rollback-contest-image-local.sh
# collect phase=restored -> 06-restored.json
```

最后在离线复核机组合六份事实。验证器要求两个不同主机、同一 session/source/bundle、严格
时间顺序、每一步座位数为零、无 pending 事务，并逐步核对正式 image ID、source release、
rollback image/source 配对及最终原基线恢复：

```bash
python3 scripts/verify_v1_cross_machine_image_evidence.py \
  --export 01-export.json --imported 02-imported.json \
  --promoted 03-promoted.json --rolled-back 04-rolled-back.json \
  --repromoted 05-repromoted.json --restored 06-restored.json \
  --expected-revision "${revision}" \
  --output v1-cross-machine-image-evidence.json
sha256sum v1-cross-machine-image-evidence.json
```

资格报告引用最后的组合 JSON，并把六份原始事实与组合 JSON 一起保存在私有证据目录。
断电/信号演练必须另行证明 pending marker 会阻断下一次未经恢复的部署；不要为了制造故障在
生产机拔电。演练机出现 marker 后，先由第二名老师记录并确认原始文件 SHA256，再运行显式恢复入口；
恢复命令只会收敛到 marker 记录的旧镜像/旧源码对，不会继续提交新版本：

```bash
marker=/opt/noi-linux-contest-system/image-promotion.pending
sudo sha256sum "${marker}"
sudo bash deploy/recover-image-promotion-local.sh \
  --expected-marker-sha256 '<上一步人工确认的64位小写SHA256>'
```

只有命令输出恢复成功，且 `verify-contest-image-from-oj.sh` 再次验证旧基线通过后，才能继续资格
演练。严禁手工删除 marker、手工改正式 Docker 标签或手工重写 `current-image-source`；重复执行
同一恢复命令只允许命中与同一 marker SHA256 绑定的回执和已恢复旧状态。

## C. 单座端到端

实际三机采集、root-only 会话初始化、每阶段采集命令和失败处理见
`deploy/V1_SINGLE_SEAT_REHEARSAL.md`。

必须逐项为真：材料发布、五项桌面合同、编译自测、手动网页回收、截止补交、OJ 原生记录、自动收卷、验证后关机。至少包括：

- CSP 路径 `考号/题目名/题目名.cpp`；
- PDF 和 2～4 组 `.in/.out` 自测数据位于规定桌面入口；
- 每次回收都从正式文件读取，OJ 创建独立记录；
- 截止比较最后确认 SHA，只在变化时补交；
- OJ 可以查看源码与成绩，学生赛后看到自己的记录；
- 关机前所有递交、通知、收卷证据和云关闭条件完成。

单座演练不能再用人工勾选的八个布尔值作为通过依据。每次真实演练生成同一个随机
`session_id`，并按严格时间顺序保存九份原始事实：

1. `01-materials.json`：控制端证明 PDF、桌面 PDF、OJ 发布回执和 2～4 组辅助数据；
2. `02-desktop.json`：桌面端证明五项入口、CSP 路径、页面 200 和 WebSocket 101；
3. `03-compile.json`：桌面端证明正式源码可编译，给定输入的输出摘要与预期一致；
4. `04-manual-submit.json`：控制端证明点击递交读取该源码并形成一个已送达 OJ RID；
5. `05-cutoff-submit.json`：截止后证明正式源码已冻结，且与上次确认版本不同时补交新 RID；
6. `06-oj-record.json`：OJ 端证明两个源码摘要、两个原生 RID、教师源码可见及学生历史可见；
7. `07-collection.json`：控制端证明归档清单、送达日志、最终源码/RID 和收卷回执；
8. `08-shutdown.json`：控制端证明同一回执已核验、入口关闭、规则归零、座位归零、云主机停止。
9. `09-test-cleanup.json`：OJ 端证明只对本 session 绑定的合成测试赛使用了 OJ 原生删除，且比赛文档、报名状态、讨论、定时任务和仍绑定该比赛的评测记录均为零。

三类采集角色必须是稳定的匿名主机身份；控制端与桌面端必须是不同主机。每份事实都包含
同一源码 revision/tree、同一不可变组件、同一匿名比赛/座位和同一个题目。`candidate_id`
只允许专门创建的合成 OJ 学生号，`seat_candidate` 则绑定运行时真实 CSP 答案目录；禁止
用真人账号替代合成身份，也禁止把两个身份混为一谈。学生 UID、用户名、token、Cookie、密码
和源码一律不进入证据。每个阶段事实还必须引用至少一份脱敏原始采集物，并在组合时从私有
证据目录逐个读取和核对 SHA256；引用只允许 `阶段/安全文件名`，不能引用绝对路径或父目录。
普通 OJ 首页、登录、准备健康和 PM2 指纹必须在八个阶段全部正常且完全不漂移。

在离线复核机组合九份事实。第 9 份必须晚于关停核验且不超过 30 分钟；删除前先保存
前 8 份原始事实及其产物。普通比赛、来源不明的比赛或仅凭标题判断的“测试赛”禁止进入
本流程：

```bash
revision="$(git rev-parse HEAD)"
python3 scripts/verify_v1_single_seat_evidence.py \
  --materials 01-materials.json \
  --desktop 02-desktop.json \
  --compile 03-compile.json \
  --manual-submit 04-manual-submit.json \
  --cutoff-submit 05-cutoff-submit.json \
  --oj-record 06-oj-record.json \
  --collection 07-collection.json \
  --shutdown 08-shutdown.json \
  --test-cleanup 09-test-cleanup.json \
  --artifact-directory /root/noi-evidence/single-seat-artifacts \
  --expected-revision "${revision}" \
  --output v1-single-seat-evidence.json
sha256sum v1-single-seat-evidence.json
```

验证器不仅检查八项为真，还强制串联：编译源码=手动递交源码；截止冻结源码必须与上次确认
源码不同；OJ 两条记录必须分别绑定这两个源码摘要；归档必须绑定截止 RID/源码；关机必须
绑定同一收卷回执，并证明合成测试赛已经及时清理。九份原始事实和组合 JSON 一起保存在私有证据目录。资格报告的
`single_seat.reference` 固定为 `single-seat-evidence.json`，其摘要必须是组合 JSON 的原始
SHA256。构建正式候选时还必须同时传入：

```bash
python3 scripts/build_v1_candidate.py --version '<semver>' \
  --qualification-report /root/noi-evidence/qualification-report.json \
  --linux-ci-evidence /root/noi-evidence/v1-linux-ci-evidence.json \
  --linux-ci-log-directory /root/noi-evidence/v1-linux-ci-logs \
  --cross-machine-evidence /root/noi-evidence/v1-cross-machine-image-evidence.json \
  --single-seat-evidence /root/noi-evidence/single-seat-evidence.json \
  --capacity-evidence /root/noi-evidence/capacity-evidence.json \
  --capacity-artifact-root /root/noi-evidence/capacity-session \
  --fault-recovery-evidence /root/noi-evidence/v1-fault-recovery-evidence.json
```

构建器会再次核对 Linux CI 十项原始日志、跨机六阶段、源码版本、四个组件、单座检查、15+2
容量 session，以及故障证据内嵌的三份 Ed25519 签名动作事实，并把组合证据封入候选；任一原始
事实尚未完成时，对应状态只能保持 `pending`。

## D. 故障恢复

逐一独立演练：控制器重启、桌面断线重连、单座换座、网络中断、收卷重试、事务 marker/断电恢复。每项都要证明：

- 不产生重复 OJ 记录或丢失最后源码；
- 不扩大到其他座位；
- 普通 OJ 不中断；
- 无法确认时学生入口关闭、ECS 停止、状态明确留待人工处理。

机器证据协议使用 `release/v1-fault-recovery-evidence.schema.json` 与
`scripts/verify_v1_fault_recovery_evidence.py`。15+2 容量证据已经承担桌面重连、单座换座和
控制器网络中断；另外三项必须由预先冻结 SHA256 的受信任动作代理实际执行，并输出同一
Ed25519 外部信任根签名的事实。验证器要求同一容量 session/source/components、同一 signer、
同一资格 marker、动作发生在一小时窗口或 30 分钟收尾期内，并拒绝重复 OJ 记录、最终源码
不一致、其他座位故障或普通 OJ 任何错误/重启/PID 变化。

控制器重启场景使用 `scripts/v1_control_restart_action_agent.py`，配置格式为
`release/v1-control-restart-action-agent-config.schema.json`，并由
`scripts/build_v1_control_restart_action_agent.py` 在 Linux root、root-only 目录中冻结为不可变执行器。
它只允许用于独立资格环境，不创建递交、不写 SQLite、不重启 Hydro/Caddy/Mongo，也不调用云 API：

1. 资格人员先创建一至一百条专用测试递交，冻结 `pending` 行的固定字段、数量和 canonical SHA256；
2. 配置同时钉死控制器完整 container/image/immutable identity、资格比赛、组件、普通 OJ 四个 PM2
   进程、三个本机 HTTP 门、OJ 内部状态查询 URL 和 root-only token 文件；
3. 执行器先核普通 OJ 与唯一控制器，随后停止该控制器；只有控制器已停止后才只读打开 SQLite，
   且要求全库 pending 集合与预先冻结集合完全一致，否则恢复同一容器并拒绝演练；
4. 它只对同一 container ID 执行 start，等待 health，通过原 SQLite 行逐条确认 `submitted` 和 RID，
   再调用 OJ 的只读幂等状态接口，以 `submission_id + payload fingerprint` 证明每条只有一个真实记录；
5. 前后普通 OJ PID/restart/status 和本机探针必须完全一致。任何歧义、源码摘要漂移、额外 pending
   项、重复 RID、重复 OJ 记录或签名验签失败都不得产生通过事实；中断时优先恢复同一控制器，
   durable marker 会保留到下一次显式恢复。

构建前不得为了“凑哈希”修改业务 SQLite；pending 集合必须来自资格动作自身创建的测试记录。
动作输出是 `v1-fault-recovery-action-fact.schema.json` 的 `control_restart` 事实，使用 namespace
`noi-v1-fault-recovery-actions` 的 Ed25519 sshsig，之后仍由组合器再次验签。

收卷重试场景使用 `scripts/v1_collection_retry_action_agent.py`，配置格式为
`release/v1-collection-retry-action-agent-config.schema.json`，构建器为
`scripts/build_v1_collection_retry_action_agent.py`。它不得在生产配置中启用，且只接受独立资格场：

- 恰好一座、一题、一个最新 `permanent_failed` 测试递交；测试行全部固定字段与 SHA256 必须匹配；
- 资格配置必须显式设置固定 `hydro.qualification_marker` 和 data 挂载内的
  `hydro.qualification_failure_marker_path`；生产配置必须省略二者；
- 代理钉死控制器 container/image/mount/config SHA，把一次性 root-only marker 原子写到已验证的
  host/container 同一映射。该 marker 只匹配一个 submission_id，在 OJ I/O 前返回明确失败，既不改
  host network 也不触碰普通 OJ；
- 第一次教师收卷必须进入 error、没有 receipt，且该行 attempts 增加并保持 permanent_failed；代理
  随后删除 marker，只允许一次教师“失败重试收卷”；
- 第二次必须进入 safe_wait，收卷根目录下只能存在一个与数据库绑定的 receipt，原 submission_id
  必须得到唯一 RID/唯一 OJ 记录，源码固定字段不得变化；普通 OJ PID/restart/status 与本机探针前后
  必须完全一致；
- 代理被中断或失败时无条件删除 marker；stale marker 状态只能恢复后拒绝新一轮，绝不带着故障开关
  继续运行。

其输出为 `collection_retry` 签名事实。真实资格执行前，必须用单独资格配置重建/重启控制器并确认
当前没有生产比赛；执行后删除资格配置和测试比赛，不能把故障字段带入发布候选。

事务 marker/断电恢复使用 `scripts/v1_power_loss_recovery_action_agent.py`，配置格式为
`release/v1-power-loss-recovery-action-agent-config.schema.json`，构建器为
`scripts/build_v1_power_loss_recovery_action_agent.py`。只能在零座位、云主机 `STOPPED`、桌面规则为零的
独立资格机执行：

- 钉死旧正式 image/source 对、新候选 image/source、发布脚本与恢复脚本 SHA256；
- 代理以两个 root-only 资格环境变量运行发布脚本。脚本只在 durable `image-promotion.pending` 已 fsync、
  image tag/source link 尚未变更的确定边界写 ready 事实并 `SIGSTOP`；生产环境未设置变量时没有此分支；
- 代理核对 ready PID、pending SHA 和旧基线后对该进程发一次 `SIGKILL`，必须观察到信号终止；
- 同一正常发布入口必须因 pending marker 明确拒绝启动；随后只调用
  `recover-image-promotion-local.sh --expected-marker-sha256 ...`，并重复一次同命令证明幂等；
- 最终必须回到旧 image/source 对，pending 消失且 recovery receipt 存在，普通 OJ PID/restart/HTTP、
  零座位、零桌面规则和云 `STOPPED` 全部保持不变。

其输出为 `power_loss_recovery` 签名事实。完整故障组合证据必须
自包含三份原始签名事实、三份动作代理 SHA256 和签名公钥；报告生成器、
候选构建器与离线候选验证器都会再次验签并与同一份 `capacity-evidence.json` 的 session/hash 交叉
绑定。不得手工构造事实，也不得把资格报告中的 `fault_recovery` 改为 `passed`。

## 独立老师安装证据

最终一项资格门使用 `release/v1-independent-teacher-install-evidence.schema.json` 和
`scripts/verify_v1_independent_teacher_install.py`。必须由没有参与候选构建的老师，在另一台干净 Linux
资格机上从候选目录开始完成安装和回滚；不能沿用开发机已展开源码，也不能在生产 OJ 上执行。

事实必须绑定候选 manifest/archive SHA、source/components、匿名主机、独立操作者摘要，并由该老师的
Ed25519 外部信任根以 namespace `noi-v1-independent-teacher-install` 签名。机器检查至少要求：候选重验、
干净目标、root-only staging、关闭前端、控制器健康、零座位、零学生规则、云 `STOPPED`、普通 OJ
零错误/零重启/零 PID 变化、回滚成功、零 pending marker；安装日志、回滚回执及普通 OJ 前后基线都以
SHA256 固定，前后普通 OJ SHA 必须相同。

资格报告构建器只有同时收到已验证的独立老师安装证据和完整故障恢复证据，才允许生成
`production_qualified=true`。候选构建器和离线候选验证器都会再次核对证据字节、source/components、
签名事实结构，并强制要求老师证据中的 `candidate.archive_sha256` 等于最终候选内同字节源码归档；
缺失或属于另一份归档时保持 pending，不得手工改报告。生产 `noictl install --plan` 还会从已经逐文件
核验的候选归档临时展开验证器，重新验证整个候选及所有外部签名，而不信任目标机工作区中的脚本。

老师不能手工拼 observation 或最终证据。第二台资格机必须从同一候选、外部 manifest 摘要和矩阵开始，
为本机干净快照生成一份**新的** `qualification-lab` 私有计划；不能复用矩阵机的私有计划、路径或
backup。按 [`V1_CLEAN_INSTALL_REHEARSAL.md`](V1_CLEAN_INSTALL_REHEARSAL.md) 的相同准备步骤建立
本机计划后，只运行最终阶段失败用例：

```bash
sudo python3 scripts/run_v1_clean_install_rehearsal_case.py \
  --private-plan "$TEACHER_PLAN" --expected-plan-sha256 "$TEACHER_PLAN_SHA" \
  --kind phase_failure --phase post_install_verification \
  --output-directory /root/noi-teacher/post-install-rollback
```

该用例会先完成六个安装阶段及 post-install 验证，再在提交前注入确定性失败并完整回滚，所以同时
证明“能够安装”和“能够恢复”；它不会把已提交安装伪装成支持卸载。随后由固定采集器验证候选、矩阵、
第二台机器、老师自己的计划和全部回执，并原子生成 root-only observation 与五个固定产物：

```bash
printf '%s' '<独立老师稳定身份>' | sudo tee /root/noi-teacher/operator-id >/dev/null
sudo chmod 0600 /root/noi-teacher/operator-id

sudo python3 scripts/collect_v1_independent_teacher_install_observation.py \
  --candidate /root/noi-candidate \
  --expected-manifest-sha256 '<来自候选目录之外可信通道的64位摘要>' \
  --private-plan "$TEACHER_PLAN" --expected-plan-sha256 "$TEACHER_PLAN_SHA" \
  --clean-install-rehearsal /root/noi-teacher/v1-clean-install-rehearsal-matrix.json \
  --teacher-case-directory /root/noi-teacher/post-install-rollback \
  --operator-id-file /root/noi-teacher/operator-id \
  --qualification-marker 'NOI-V1-TEACHER-<本轮随机标识>' \
  --confirm-independent INDEPENDENT-TEACHER-CLEAN-INSTALL-AND-ROLLBACK \
  --output-directory /root/noi-teacher/sealed-observation
```

采集器使用矩阵的 session salt 重新计算本机匿名身份；如果矩阵实际也在本机生成，会直接拒绝。
它还要求老师用例完成全部六阶段、精确停在 `post_install_verification` 失败边界、最终
`rollback_verified`、普通 OJ 前后字节相同且没有 pending marker。输出目录不存在时才允许创建，
完成后固定为 `teacher-install-observation.json` 和 `artifacts/`。

随后老师在资格机用自己的私钥签署：

```bash
sudo python3 scripts/build_v1_independent_teacher_install_evidence.py \
  --observation /root/noi-teacher/sealed-observation/teacher-install-observation.json \
  --artifact-directory /root/noi-teacher/sealed-observation/artifacts \
  --candidate /root/noi-candidate \
  --expected-manifest-sha256 '<来自独立可信发布通道的64位摘要>' \
  --signer '<independent-teacher-id>' \
  --signing-key /root/noi-teacher-signing-key \
  --signing-public-key 'ssh-ed25519 AAAA...' \
  --output /root/noi-evidence/v1-independent-teacher-install-evidence.json
```

构建器要求 Linux root、root-only 输入、五个固定产物、普通 OJ 前后字节完全相同；它会逐项核验
受外部摘要钉住的归档并在私有临时目录展开整套源码，再运行其中的候选验证器，
私钥和公钥匹配，并会在写出前自行验签。它不接受日志中的人工布尔值替代 observation 的机器门。
`--expected-manifest-sha256` 必须从候选目录之外的发布签名页或第二条可信通道取得；读取候选自己
声明的摘要再填入不构成信任。老师的签名私钥不得从构建机复制，也不得由候选构建者代签。

## E. 普通 OJ 隔离

整个单座和 15+2 窗口持续记录普通 OJ 首页、登录、准备健康、Hydro/Caddy/Mongo/PM2 PID 与 restart。资格门要求应用错误、服务重启、PID 变化均为 0。公网探针抖动必须与服务器本机探针和独立外部控制交叉判断，不能把客户端网络波动误记为服务通过或失败。

## F. 15+2 一小时长稳

固定 15 个正式座位和 2 个备用座位，持续至少 3600 秒：

1. 15 人同时登录、打开 PDF、自测数据和编辑器；
2. 分三轮完成编译、自测和网页回收；
3. 至少一座执行换座并保持源码一致；
4. 中途重启一个桌面容器、短断控制器网络并恢复；
5. 到点自动冻结并回收全部正式座位；
6. 核对每名学生每题最后一次源码、OJ 记录与收卷证据；
7. 撤入口、撤学生规则并等待 ECS `Stopped`；
8. 普通 OJ 全程隔离门保持为 0。

容量结论不得再由人工只填几项数字得出。完整结果写入固定的
`capacity-evidence.json`，按 `release/v1-capacity-evidence.schema.json` 记录并绑定：

- 精确源码 revision/tree、控制镜像、桌面镜像、桌面源码 revision 和 Hydro 插件；
- 实例规格、地域、网络画像摘要、开始/结束时间、采样间隔与完整原始采样数量；
- 15 个正式座位、2 个备用座位、17 个唯一容器和 17 座全部验收；
- 三轮共至少 45 次编译和 45 次递交、15 人全部收卷、最终源码零不一致；
- 至少一次换座、一次计划内桌面重启、一次控制器网络中断，而且每次都必须有对应恢复；
- 非计划桌面重启、跨座访问、普通 OJ 错误/重启/PID 变化、凭据泄露和结果泄露全部为 0；
- 结束后活动座位、托管规则、冲突规则、递交/通知队列均为 0，云主机为 `STOPPED`；
- CPU、内存、出口带宽、RTT、丢包、WebSocket 重连、按键到画面 p50/p95 实测值与预先冻结的阈值；
- 六类私有原始证据文件的相对引用、字节数和 SHA256。

六类文件不是任意附件：必须是 UTF-8 JSON 对象，使用固定 `schema_version=1`、固定 `kind`
和同一个 `session_id`。验证器会从 `sample_series` 重新计算性能峰值与 WebSocket 重连总数，
从 `seat_inventory` 重新计算正式/备用/已验容器集合，并把 `workload_events`、`fault_events`、
`ordinary_oj_observations`、`shutdown_observation` 的固定字段与组合证据逐项对齐。只改组合摘要、
只换附件哈希或只复制旧会话文件都不能通过。

先在私有证据目录执行：

正式采集必须按 `deploy/V1_CAPACITY_REHEARSAL.md` 初始化不可变会话，以受信任的只读测量探针
完成固定 cadence 采样，再由五个受信任终态探针记录座位、工作负载、故障、普通 OJ 隔离和
关停事实。人工 JSON 只能做格式预检，不能被终结为生产资格证据。

```bash
python scripts/verify_v1_capacity_evidence.py \
  capacity-evidence.json \
  --artifact-root /root/noi-evidence
```

硬门：`formal_seats=15`、`spare_seats=2`、`duration_seconds>=3600`、非计划座位重启=0、
递交失败=0、收卷失败=0、17 座全部验证、换座/计划重启/控制器断网都至少成功恢复一次、
性能实测均不超过预先冻结阈值、容量余量由两名不同复核者接受。计划内重启不是异常重启；
两者必须分别记录。任何镜像、源码、实例规格、网络画像或阈值改变后，该容量证据失效。
