# NOI Linux V1：15+2 一小时容量验收

本手册只用于独立资格环境，不用于正在上课或比赛的玄武 OJ。容量验收不会自动获得生产资格，
也不能替代跨机器镜像导入/回滚和单座全流程验收。

## 1. 安全边界

- 采集命令只允许在 Linux root 下运行；会话目录及祖先必须 root 所有、无 group/world 写，
  会话目录必须 `0700`，文件必须 `0600`。
- 采集器不含 Docker restart/stop、PM2 restart、云写 API 或网络规则修改。计划内桌面重启、
  换座和控制器断网按独立演练步骤由操作员执行，采集器只读取受信任探针的结果。
- 探针必须是绝对规范路径、root:root、单硬链接、不可被 group/world 写，祖先目录同样不可写；
  采集器用洁净环境执行，拒绝 stderr、非零退出、超时、重定向式替代文件和非 JSON 输出。
- 正式资格的所有 `fact` 必须使用 `--probe`。`--input` 仅允许本地单元测试、事故复盘和格式
  预检，不得写入正式资格会话。
- 原始事实只保存容器 ID、计数、状态、性能值和摘要，不保存学生姓名、用户名、token、密码、
  Cookie、源码、题目隐藏数据、AccessKey 或 Mongo URI。

## 2. 冻结身份与阈值

创建 root-only 的 `identity.json`，精确字段为：

```json
{
  "source": {"revision": "40位Git提交", "tree": "40位Git树"},
  "components": {
    "orchestrator_image_digest": "sha256:...",
    "desktop_image_id": "sha256:...",
    "desktop_source_revision": "40位Git提交",
    "hydro_plugin_sha256": "64位SHA256"
  },
  "environment": {
    "profile": "aliyun-hydro5-pm2-direct-v1",
    "instance_type": "实际实例规格",
    "region": "实际地域",
    "network_profile_sha256": "冻结网络路径说明的SHA256"
  },
  "probes": {
    "measurement": "性能测量探针SHA256",
    "seat_inventory": "座位清单探针SHA256",
    "workload_events": "工作负载探针SHA256",
    "fault_events": "故障演练探针SHA256",
    "ordinary_oj_observations": "普通OJ终态探针SHA256",
    "shutdown_observation": "安全关机探针SHA256"
  },
  "thresholds": {
    "host_cpu_peak_percent_max": 85,
    "host_memory_peak_percent_max": 85,
    "container_memory_peak_bytes_max": 3221225472,
    "egress_peak_mbps_max": 100,
    "rtt_p95_ms_max": 100,
    "packet_loss_percent_max": 1,
    "websocket_reconnects_max": 3,
    "key_to_frame_p95_ms_max": 250,
    "thresholds_sha256": "按下述命令计算",
    "capacity_margin_accepted": true
  }
}
```

阈值必须在开始采样前由两名复核者批准。将八个 `*_max` 字段单独保存为
`threshold-values.json`，计算确定性摘要：

```bash
sudo python3 scripts/collect_v1_capacity_evidence.py threshold-sha256 \
  --input /root/noi-v1-capacity/threshold-values.json
```

把输出写回 `identity.json` 后，不得在会话中修改阈值。验证器会从八个数值重新计算摘要，
旧摘要、随手填写的 64 位字符串和采样后放宽阈值都不能通过。
六个探针也必须在 `init` 前全部构建、计算 SHA256 并写入 `probes`；会话开始后实际执行文件
只要与冻结摘要不同，采样或事实记录会当场失败。最终组合证据公开保留这些摘要，但不保留探针
路径、学生链接或任何凭据。

## 3. 探针合同

### 3.1 定时测量探针

每次执行只向 stdout 输出一个 JSON 对象，stderr 必须为空：

```json
{
  "observed_at": "2026-08-12T00:00:00Z",
  "telemetry": {"sequence": 1, "sha256": "64位SHA256"},
  "ordinary_oj": {
    "schema_version": 1, "qualification_marker": "NOI-V1-QUAL-...",
    "sequence": 1, "observed_at": "2026-08-12T00:00:00Z",
    "homepage_status": 200, "login_status": 200,
    "prep_health_ok": true, "prep_database_ok": true,
    "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
    "ordinary_oj_pid_changes": 0, "credential_leaks": 0, "result_leaks": 0,
    "pm2_fingerprint_sha256": "64位SHA256", "sha256": "64位SHA256"
  },
  "metrics": {
    "host_cpu_peak_percent": 0,
    "host_memory_peak_percent": 0,
    "container_memory_peak_bytes": 1,
    "egress_peak_mbps": 0,
    "rtt_p95_ms": 1,
    "packet_loss_percent": 0,
    "websocket_reconnects": 0,
    "key_to_frame_p50_ms": 1,
    "key_to_frame_p95_ms": 1
  }
}
```

测量值必须来自同一采样周期：宿主 CPU/内存、17 个目标容器中的最大内存、主网卡出口、
杭州真实学生链路 RTT/丢包、真实 noVNC WebSocket 重连数和按键到画面 p50/p95。若浏览器
测量端失联或任一数据缺失，探针必须非零退出，不能沿用上一次值或填 0。

同一轮还必须绑定普通 OJ 主机独立签名的观察：匿名首页、登录页、`/prep/health` 全部健康，
`caddy`、`hydro-sandbox`、`hydrooj`、`mongodb` 四个 PM2 进程的 PID、restart_time 和 online 状态
与开窗前冻结基线完全一致，并且凭据/结果金丝雀没有出现在公共响应中。该观察器部署在 OJ 主机，
不与比赛 ECS 或杭州浏览器代理共用私钥。任一轮没有新的单调 sequence、复用旧 SHA、PM2 指纹变化
或观察超过 60 秒，当前容量样本立即 NO-GO；因此结论覆盖整整一小时，而不是只看结束快照。

### 3.2 五类终态探针

固定 `kind` 为：

- `seat_inventory`：15 个正式容器 ID、2 个备用容器 ID、17 个已验容器 ID、非计划重启和跨座访问失败；
- `workload_events`：登录、材料打开、编译、递交、收卷和最终源码不一致计数；
- `fault_events`：换座、计划重启、控制器断网及对应恢复计数；
- `ordinary_oj_observations`：普通 OJ 错误、重启、PID变化、凭据泄露、结果泄露计数；
- `shutdown_observation`：活动座位、安全组托管/冲突规则、云状态、递交和通知队列。

每个探针输出仅含该类固定字段及 `observed_at`。事实分两阶段取得：`seat_inventory` 与
`fault_events` 必须在最后一个性能样本后 5 分钟内、自动收卷开始前完成；其余三类在收卷、30 分钟
保护期和安全关机全部完成后取得，最晚不超过最后样本后 45 分钟。这样既能在容器仍运行时证明
17 座与故障恢复，又能在删除座位后证明最终关停；终结器和候选回读验证器会分别拒绝错阶段、
过早/过期时间、字段缺失、多余字段、负数、重复容器和跨会话事实。

`seat_inventory` 不允许再由人工清单生成。先按
`release/v1-capacity-seat-inventory-probe-config.schema.json` 冻结 15 个正式和 2 个备用的完整容器
ID、slot、PID、StartedAt、RestartCount、镜像与 Docker 网络 ID，并在开窗前指定唯一一个正式座位
作为计划重启目标（`restart_count_delta=1`），另指定一个不同的正式座位作为故障换座目标，再用
`scripts/build_v1_capacity_seat_inventory_probe.py` 构建单文件探针。探针只调用 Docker GET inspect、
读取 17 个内部 IP 的 `/vnc.html`，并要求内部 bridge、ICC=false、网络中无第 18 个容器；任一容器
换镜像、换 slot、noVNC 不健康或网络混入都会 NO-GO。终态要求计划重启目标 RestartCount 精确增加1、
PID/StartedAt 更新且恢复运行；故障座位的旧容器必须消失，并由同 slot、同镜像、同隔离网络、
RestartCount=0 的全新容器恢复；其余15座的生命周期必须与开窗基线完全相同。故障换座成功后，
控制器会先保留学生在已验收备用位，再用新凭据和空答案目录重建原槽位；若重建失败，后台明确显示
备用容量降级并提供幂等修复动作，资格报告不得把只有“学生已接管”误写成“15+2 已恢复”。

`shutdown_observation` 也不允许人工填 0。按
`release/v1-capacity-shutdown-probe-config.schema.json` 冻结控制器完整容器 ID、镜像、PID、StartedAt、
RestartCount、Docker socket 和 HTTPS `/healthz`，再用 `scripts/build_v1_capacity_shutdown_probe.py`
生成 root-only 单文件探针。它只调用 Docker GET 与 HTTPS GET，并要求控制器身份未变化、活动座位为0、
比赛云主机 `STOPPED`、入口规则0/0、递交及通知队列全部清空；任一字段缺失或漂移立即 NO-GO。

`workload_events` 同样不能由演练人员填写八个计数。先按
`release/v1-capacity-workload-probe-config.schema.json` 冻结本场 tid、submission session、三道题、
15 个 `slot_no -> uid` 绑定、独立工作负载执行端的 Ed25519 公钥和签名信封，以及本次尚未创建但
路径已固定的 root-only `capacity_session_dir`，再用
`scripts/build_v1_capacity_workload_probe.py` 构建单文件探针。执行端信封只允许声明 1..15 全部座位
完成登录和材料打开，并列出严格的 15×3 编译矩阵；不能含用户名、token、密码、源码或输出内容。
探针随后自行以 SQLite `mode=ro` + `query_only` + `integrity_check` 读取控制器数据库，要求本场恰好
15 个冻结 uid、三题各一次共 45 条 `realtime/submitted` 记录，submission ID、RID、源码摘要和运行
session 全部有效。最后读取数据库绑定的 root-only 收卷目录，复验 receipt SHA、六个证据文件摘要、
完整 archive manifest、15×3 CSP 正式源码路径，并逐条要求最终源码 SHA 与已确认递交一致、
`submit_log` 复用同一个 RID。登录/材料/编译、实时递交、截止源码、OJ 回执四条来源缺少任意一条，
探针都会 NO-GO；它还会从容量会话读取完整首尾样本，要求负载动作开始和完成都位于同一个一小时
采样窗口内，不能用窗口外或上一场的合法回执补证。它不会读取或输出明文源码及学生身份。

```bash
sudo python3 scripts/build_v1_capacity_workload_probe.py \
  --config /root/noi-v1-workload-probe-config.json \
  --output /root/noi-v1-probes/workload_events
sudo sha256sum /root/noi-v1-probes/workload_events
```

工作负载执行端与探针是两个角色：执行端可以在资格环境中登录、打开材料、编译和递交，探针只做
终态只读核验。不能用探针进程自己生成签名信封，也不能把执行端私钥放到控制器、收卷目录或仓库。
正式开窗前必须做三次负向测试：缺一个座位、缺一个编译 pair、篡改一个收卷 RID，三者都必须失败。

执行端也不能由操作员手工填写 `login_slots/material_open_slots/compile_pairs`。先运行上一节独立非 root
浏览器代理，取得当前 15 个冻结 URL 均已建立 noVNC 画面和 WebSocket 的签名信封；再按
`release/v1-capacity-workload-action-agent-config.schema.json` 冻结15个正式座位的 container ID/name、
镜像、PID、StartedAt、RestartCount、考号、三道题和浏览器 signer，由
`scripts/build_v1_capacity_workload_action_agent.py` 构建 root-only 单文件执行端。它先验浏览器签名和
时效，再让15座通过桌面 Evince 打开题面，并用同步屏障并发执行座位内
`/usr/bin/g++ -std=c++14 -O2 -pipe`，逐座编译正式答案目录里的三份源码；证据要求编译在途峰值精确
达到15，编译产物只写 `/tmp` 并逐份删除；每座执行前后都要求完整 Docker 生命周期未变化。
最终只输出 slot/problem、容器身份摘要、计数、时间和 Ed25519 签名，不读取或输出源码、考号、URL、
token、用户名和密码。其 receipt 原始 SHA 被签名 payload 绑定，终态只读探针必须同时验 receipt 与信封。
构建器输出的 `agent_sha256` 必须先写入 workload probe 配置的 `action_agent_sha256`，再构建只读探针；
执行端运行时重新哈希自身文件并把摘要写入 receipt，防止只凭一把可复用私钥伪造动作证据。

```bash
sudo python3 scripts/build_v1_capacity_workload_action_agent.py \
  --config /root/noi-v1-workload-action-agent-config.json \
  --output /root/noi-v1-probes/run-workload-actions
sudo /root/noi-v1-probes/run-workload-actions
```

`fault_events` 也必须由三条不同来源交叉推导，不能人工写六个 `1`。先按
`release/v1-capacity-fault-probe-config.schema.json` 冻结本场 tid、故障前 pool revision、故障正式槽位、
接管备用槽位、已冻结的 `seat_inventory` 探针 SHA，以及独立网络故障执行端的 signer、公钥、目标摘要、
签名信封路径和同一个 `capacity_session_dir`，再用 `scripts/build_v1_capacity_fault_probe.py` 构建单文件探针：

```bash
sudo python3 scripts/build_v1_capacity_fault_probe.py \
  --config /root/noi-v1-fault-probe-config.json \
  --output /root/noi-v1-probes/fault_events
sudo sha256sum /root/noi-v1-probes/fault_events
```

探针以 SQLite `mode=ro`、`query_only` 和 `integrity_check` 读取座位池，要求故障前 revision 后恰好连续
出现一次 `replace`、一次原槽位 `repair:warm` 和一次 `repair:verify`；备用座位保留原学生绑定，原槽位
恢复为未绑定、`failure_count` 不归零的已验座位，资源表重新达到17条，学生表仍为15条。然后它以
固定 SHA 和洁净环境实际运行同一个 `seat_inventory` 探针，要求计划重启严格一次、故障旧容器被唯一
新容器替代、17座全部健康。最后验证 namespace `noi-v1-capacity-network-fault` 的 Ed25519 信封；信封
必须包含目标可达连续3次、隔离期间连续失败3次、恢复后连续成功3次，三阶段时间严格递增且总跨度
不超过10分钟。只有三条链全部成立，才输出换座、容量恢复、计划重启、计划重启恢复、控制器断网和
断网恢复各一次；断网开始与恢复完成也必须位于完整首尾样本之间。输出不含 uid、用户名、容器 ID、
地址或故障命令。

网络故障执行端与故障探针也必须分权：执行端只对冻结的控制器探测目标做有界 egress deny/restore，
私钥不进入控制器或仓库；探针只读签名信封，不能自行制造断网记录。开窗前至少做缺少 repair receipt、
多一个网络成员、断网只有2次连续失败、错签名四个负向测试，全部必须 NO-GO。

执行端按 `release/v1-capacity-network-fault-agent-config.schema.json` 冻结控制器完整 Docker 身份、比赛
主机唯一 IPv4/TCP 探测目标、`nsenter`/`iptables`/Python/`ssh-keygen` 绝对路径、signer、公私钥和
root-only 恢复/回执/信封路径，再由 `scripts/build_v1_capacity_network_fault_agent.py` 构建权限0500的
单文件工具。工具先做真实签名＋验签自检，然后进入固定控制器 PID 的网络 namespace，只安装一条
带资格 marker 的精确 OUTPUT TCP REJECT；普通 OJ 主机 namespace 不变。它在任何规则写入前后重复
核对控制器 container/image/PID/StartedAt/RestartCount，持有 nonblocking root-only 锁；HUP/INT/TERM
都会进入清理，遗留的持久化 recovery state 会使下次运行只撤销规则并拒绝继续，避免断网证据重复。

```bash
sudo python3 scripts/build_v1_capacity_network_fault_agent.py \
  --config /root/noi-v1-network-fault-agent-config.json \
  --output /root/noi-v1-probes/run-controller-network-fault
sudo /root/noi-v1-probes/run-controller-network-fault
```

同样把网络执行端构建器输出的 `agent_sha256` 写入 fault probe 配置的
`network_action_agent_sha256` 后再构建故障只读探针。网络执行端运行时哈希自身并签入 receipt；
代理文件、receipt 或信封任何一个被替换都会 NO-GO。

### 3.3 冻结性能探针

不能把可修改的 `config.json` 放在探针旁边，再声称只固定了脚本 SHA。先创建权限 `0600` 的
`v1-capacity-probe-config.json`，字段严格遵循
`release/v1-capacity-probe-config.schema.json`：

- `container_ids` 必须是本次 15 个正式座位和 2 个备用座位的 17 个完整容器 ID；
- `network_interface` 是专用比赛主机真实出口网卡；
- `telemetry_envelope` 位于 root-only 目录，由杭州独立浏览器代理以单文件原子更新；信封同时包含
  payload、签名、namespace 和 signer，避免 JSON 与签名跨文件复制时短暂错配；
- `qualification_marker` 是本次演练随机身份，绑定浏览器和普通 OJ 两条签名链；
- `telemetry_seat_set_sha256` 与浏览器配置中的 `seat_set_sha256` 必须相同。摘要由排序后的 15 个
  完整座位 URL、RTT URL、WebSocket 规则和 `direct_http` 模式组成；签名 payload 每轮携带摘要，
  但不携带任何座位 token。这样无需给生产网关增加资格专用响应头，也不会把普通学生座位误认成
  本次固定的 15 个资格座位；
- `telemetry_transport_profile` 正式容量测试必须是 `direct_http`，并绑定本次杭州 EIP 的 IPv4/80；
  `compat_https` 只能作为备用链路的对照报告，不能替代真实学生高速入口的 15+2 容量结论；
- `telemetry_public_key` 只放独立浏览器代理的 Ed25519 公钥；私钥绝不能进入服务器、仓库或
  探针配置；
- `ordinary_oj_envelope` 是 OJ 主机独立观察器发布到控制机的 root-only 信封；相应 signer、公钥、
  PM2 基线摘要和最大年龄也冻结进探针；
- `measurement_seconds` 建议 5，且必须小于定时采样间隔；
- 浏览器遥测每次至少包含 5 个 RTT 和按键到画面样本、丢包率及本窗口 WebSocket 重连数。

浏览器代理必须部署在杭州链路的一台独立 Linux 主机上，以专用非 root 用户运行；它同时打开
15 个互不相同的正式资格座位并保持 15 条 noVNC WebSocket，每一轮对 15 个画布并发发键采帧；
2 个备用位保持预热、运行和资源采样，但不冒充学生负载。代理不能与比赛
服务器、控制器或采集器共用主机、私钥或 root 账户。代理固定 Node 22--24、锁文件中的 Playwright
版本和系统 `ssh-keygen`。`direct_http` 只接受 IPv4/80，`compat_https` 只接受 HTTPS；每个 RTT 请求
也必须返回 HTTP 200。15 个 URL、RTT URL 和 WebSocket 规则已由 `seat_set_sha256` 签名绑定；
HTTP 档位反映当前真实学生路径的已知明文风险，不应被误写为安全
链路；长期方案仍是把专用域名和 TLS 终止下沉到杭州 ECS。
代理监听全部 15 条 noVNC WebSocket，并通过 Playwright 的键盘接口向每个 `#noVNC_container canvas` 发送
固定按键；画布下一帧
时间来自浏览器同一 `performance.now()` 时钟，不能用脚本主机时钟替代。

代理将规范 JSON（键排序、紧凑分隔符、末尾换行）以 namespace
`noi-v1-capacity-telemetry` 生成 OpenSSH 签名。`sequence` 必须单调递增。控制主机上的探针拒绝
过期、未来时间、资格标记不一致、非规范 JSON、错误签名、重复 sequence/SHA 或缺样，绝不沿用旧值。

独立主机安装时先按 `release/v1-capacity-browser-agent-config.schema.json` 创建权限 `0600` 的配置，
再执行：

```bash
sudo useradd --system --create-home --home-dir /var/lib/noi-v1-browser noi-v1-browser
sudo install -d -o noi-v1-browser -g noi-v1-browser -m 0700 /var/lib/noi-v1-browser
sudo -u noi-v1-browser npm ci --omit=dev
sudo -u noi-v1-browser npx playwright install chromium
sudo -u noi-v1-browser node agent.mjs \
  --config /var/lib/noi-v1-browser/agent-config.json --loop
```

真实服务化部署还要用 systemd 固定 `User=noi-v1-browser`、`NoNewPrivileges=yes`、只写
`/var/lib/noi-v1-browser`，并把签名私钥、sequence 和信封目录限制为该用户 `0700/0600`。在测试
网关确认资格响应头之前不要启动代理；本仓库不会自动给普通座位添加这个标记。

跨机器发布不能用共享目录、密码 SSH 或 `scp` 覆盖控制机文件。控制机给一个专用 SSH key 配置
`authorized_keys` 的 `restrict,command="..."` 强制命令；该命令只运行
`scripts/install_v1_capacity_telemetry.py`，从 stdin 读取信封、复验 Ed25519 签名和 `direct_http`，
要求 sequence 严格增加，再以 root:root 0600 原子安装。杭州代理只调用 `publish.mjs`，SSH 必须
显式传入专用 identity 和 pinned `known_hosts`，并使用 BatchMode、禁密码、禁交互认证、禁转发；
成功输出只能是 `TELEMETRY_INSTALLED sequence=N`。
控制机不持有代理私钥，杭州代理也不获得控制机 shell。建议的强制命令形态：

```text
restrict,command="sudo -n /usr/local/sbin/install-v1-capacity-telemetry" ssh-ed25519 AAAA... capacity-agent
```

其中 root-owned `/usr/local/sbin/install-v1-capacity-telemetry` 固定 installer SHA、输出路径、signer、
公钥和 `/usr/bin/ssh-keygen`，不接受 SSH 客户端传入参数。正式采样前必须分别做旧 sequence 重放、
错签名和非 direct profile 三次负向测试，三者都应 NO-GO。

普通 OJ 观察器按 `release/v1-capacity-ordinary-oj-agent-config.schema.json` 冻结 HTTPS origin、公共
路径、四个 PM2 基线、两条一次性金丝雀和签名身份，再用
`scripts/build_v1_capacity_ordinary_oj_agent.py` 构建 root:root 0500 单文件。配置和运行目录均须 0700，
私钥、非阻塞运行锁、状态和信封须 0600。每 10 秒由 systemd timer 或专用循环运行一次；锁已被
上一实例持有时必须立即 NO-GO，不能并发分配 sequence。随后使用
`scripts/publish_v1_capacity_ordinary_oj_telemetry.py` 通过受限 SSH 把信封原子送到控制机；强制命令必须固定 ordinary-OJ namespace、signer、公钥
和输出路径，不能给予远程 shell。开窗前还要做 PM2 restart_time 改动 fixture、旧 sequence、错误
签名和泄漏金丝雀四次负向测试，全部必须失败关闭。

在 root-only 目录构建单文件探针：

```bash
sudo install -d -o root -g root -m 0700 /root/noi-v1-probes
sudo python3 scripts/build_v1_capacity_probe.py \
  --config /root/noi-v1-capacity-probe-config.json \
  --output /root/noi-v1-probes/measure-capacity
sudo sha256sum /root/noi-v1-probes/measure-capacity
```

构建器把配置直接嵌入权限 `0500` 的单文件探针；后续采集器记录的 probe SHA 同时绑定脚本、
17 个容器、网卡、公钥和遥测位置。探针只执行 Docker Unix socket 的 `GET /containers/.../json`，
再从对应只读 cgroup 读取内存；它不会调用容器或云写接口。采样周期内每秒读取宿主 CPU、内存、
出口和 17 个 cgroup 值，输出各自峰值。开始和结束还会证明所有容器 PID 未变化且仍为运行态。

正式演练前必须先人工执行探针两次，确认两次 JSON 的 telemetry sequence/SHA 不同；否则浏览器
代理没有真正持续采样，不能开始一小时窗口。

## 4. 执行顺序

先按 `release/v1-capacity-rehearsal-guard-config.schema.json` 创建 root-only 守门配置，固定 identity、
session、六个探针、两个动作执行端的路径/构建 SHA 和四个动作输出路径。每个阶段开始前都运行：

```bash
sudo python3 scripts/v1_capacity_rehearsal_guard.py \
  --config /root/noi-v1-capacity/rehearsal-guard.json
```

守门器只读文件和摘要，不调用 Docker、云、网络、PM2、OJ 或控制器写接口。它只返回
`initialize -> sampling_and_actions -> runtime_facts -> terminal_facts -> finalize -> independent_verification`
中的唯一阶段和下一步；探针/执行端摘要漂移、动作 receipt/envelope 只出现一半、事实提前出现、额外文件、
样本超过计划、最后一个样本出现时两项动作尚未完成，或 session 与 identity 不一致都会 NO-GO。负载动作与
断网恢复动作必须在固定节奏采样期间完成，不能等采样结束后补做。它不会把“已生成 capacity-evidence.json”直接说成
通过；最后仍必须运行独立验证器并由不同复核者签字。

```bash
sudo install -d -o root -g root -m 0700 /root/noi-v1-capacity

sudo python3 scripts/collect_v1_capacity_evidence.py init \
  --identity /root/noi-v1-capacity/identity.json \
  --session-dir /root/noi-v1-capacity/session \
  --duration-seconds 3600 \
  --sample-interval-seconds 10

sudo python3 scripts/collect_v1_capacity_evidence.py run \
  --session-dir /root/noi-v1-capacity/session \
  --probe /root/noi-v1-probes/measure-capacity
```

`run` 从空会话开始，按固定 cadence 采集 361 个边界样本。任何探针失败、超过 30 秒、采样
迟到超过 2 秒、时间不递增或进程中断都会 NO-GO；不得删除中间文件后续跑。应新建完整会话。
每个样本还保存浏览器和普通 OJ 两条已签名遥测的 sequence 与 SHA；最终器及候选回读会再次拒绝
任何重放、倒退、PM2 指纹变化或非零普通 OJ 故障/泄漏计数。

在一小时窗口内完成三轮工作负载、一次换座、一次计划重启和一次控制器网络中断。第 361 个样本
成功后立即停止学生动作，并在 5 分钟内、收卷前先记录仍需运行中座位的两类事实：

```bash
for kind in seat_inventory fault_events; do
  sudo python3 scripts/collect_v1_capacity_evidence.py fact \
    --session-dir /root/noi-v1-capacity/session \
    --kind "$kind" \
    --probe "/root/noi-v1-probes/$kind"
done
```

随后触发自动收卷。系统暂停座位、生成回收凭据并等待默认 30 分钟保护期；确认安全关机完成后，
在最后样本后 45 分钟内记录其余三类事实：

```bash
for kind in workload_events ordinary_oj_observations shutdown_observation; do
  sudo python3 scripts/collect_v1_capacity_evidence.py fact \
    --session-dir /root/noi-v1-capacity/session \
    --kind "$kind" \
    --probe "/root/noi-v1-probes/$kind"
done

sudo python3 scripts/collect_v1_capacity_evidence.py finalize \
  --session-dir /root/noi-v1-capacity/session

sudo python3 scripts/verify_v1_capacity_evidence.py \
  /root/noi-v1-capacity/session/capacity-evidence.json \
  --artifact-root /root/noi-v1-capacity/session
```

`finalize` 可在“证据已经完整落盘但终端未收到成功输出”时安全重跑：它只读回并重新验证已有
结果，绝不覆盖。任何中间文件内容不同、缺样或证据不一致都会失败关闭。

## 5. 通过与清理

只有验证器退出 0、资格报告摘要与 `capacity-evidence.json` 完全一致、两名不同复核者确认
性能余量，才可把容量门改为 `passed`。构建候选时还必须传入：

```bash
--capacity-evidence /root/noi-v1-capacity/session/capacity-evidence.json \
--capacity-artifact-root /root/noi-v1-capacity/session
```

资格测试比赛完成后，必须先用 OJ 原生删除功能删除测试比赛，并用单座资格流程的只读清理观察
证明最终无比赛、讨论、报名状态及绑定记录；不能因容量证据存在而保留无用测试比赛。私有证据
按保留策略归档，不进入公开源码候选。
