# 部署与首次联调

## 1. 比赛服务器

购买按量实例后，先在控制台确认：

- Ubuntu 22.04；OJ 主机 `/32` 的 TCP 22/80 作为静态管理与回退规则，学生 TCP/80 由编排器按比赛生命周期临时管理。
- 已启用停机不收费/节省停机；系统盘仍会持续计费。
- 旧 XFCE 镜像的容量数据不能套用到官方 GNOME 镜像。
- 当前 16C64G 只按“不超过 15 人的待验收目标”配置；正式使用前须做 15 路、30～60 分钟的真实操作压测。8C32G 也必须单独压测，不给未经验证的人数承诺。

上传本项目和官方 ISO 后运行（也可在比赛服务器直接从 NOI 官方地址下载 ISO）：

```bash
sudo bash deploy/bootstrap-contest-server.sh
sudo apt-get install -y squashfs-tools libarchive-tools
sha256sum ubuntu-noi-v2.0.iso
candidate="noi-linux-official:candidate-$(date -u +%Y%m%dT%H%M%SZ)"
source_revision='<发布包标注的40位小写Git提交ID>'
sudo bash deploy/build-noi-official-image.sh \
  ubuntu-noi-v2.0.iso "${candidate}" "${source_revision}"
sudo bash deploy/verify-contest-image-local.sh \
  "${candidate}" "${source_revision}"
docker image inspect noi-linux-official:2.0 >/dev/null 2>&1 \
  && docker tag noi-linux-official:2.0 \
    "noi-linux-official:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
docker tag "${candidate}" noi-linux-official:2.0
```

ISO 的期望 SHA256 是 `c8824240736352e5e4aaf3f6532b40961f75fa9f23d670bb78881355a49d5878`。构建脚本会把该来源和显式提供的源码 revision 写入镜像标签；摘要和 revision 都以小写十六进制写入，revision 必须是发布包标注的 40 位小写提交 ID，不能在比赛服务器上临时猜测 `HEAD`。缺失、格式错误或镜像标签不匹配时验收立即失败。新版本必须先构建为候选标签，通过三种提交模式验收后才能提升为 `noi-linux-official:2.0`，旧正式镜像保留为回滚标签。OJ 服务器上的 `deploy-contest-image-from-oj.sh` 执行同一流程，但调用前还必须显式设置 `NOI_SOURCE_REVISION`：

```bash
source_revision='<发布包标注的40位小写Git提交ID>'
contest_host_key_sha256='<从可信通道核对的SHA256主机指纹>'
sudo NOI_SOURCE_REVISION="${source_revision}" \
  CONTEST_SSH_HOST_KEY_SHA256="${contest_host_key_sha256}" \
  bash deploy/deploy-contest-image-from-oj.sh <比赛服务器IP>
```

包装脚本会把该值原样传到比赛服务器，在复用缓存 rootfs 和从 ISO 重建的两个分支中都绑定 `org.opencontainers.image.revision`，并在候选镜像的不可变 ID 上精确比较后才运行桌面验收。完整的 15 路标准见 `ARCHITECTURE_AND_PERFORMANCE.md`。

每次提升都会把正式镜像 ID 与对应源码快照成对记录。需要回滚时，只在 **OJ 服务器**执行 `sudo bash /opt/noi-linux-contest-system/deploy/rollback-contest-image-from-oj.sh <比赛服务器IP>`。包装脚本会先取得与备赛共用的锁，再通过 SSH 调用比赛机上的本地回滚实现；不要登录比赛机直接运行本地脚本，也不能只手工改一个 Docker 标签。

首次把已有正式镜像纳入这套发布管理时，部署脚本会把比赛机上原有构建源码做成只读基线快照，并在 `promotion.env` 标记 `BASELINE_UNVERIFIED=1`。它不会拿新版桌面断言去阻断旧镜像；这个基线只用于首次升级的应急回退，不能当成已通过新版验收的正式候选。

发布和回滚切换前会写入 `/opt/noi-linux-contest-system/image-promotion.pending`，成功配对或信号回滚成功后才删除。若该文件仍存在，部署、验收和回滚都会停止；此时必须先确认没有 `noi.contest` 座位运行，按文件中的 `OLD_IMAGE_ID` 与 `OLD_SOURCE_TARGET` 显式恢复并核对目标 release 的 `promotion.env`，再删除标记。不要为了继续发布而直接删标记。

旧的 `noi-linux-sim:latest` 镜像只用于紧急回退。正式验收通过前不要删除它；切换镜像只需修改 `contest_server.docker_image` 并重新登记/备赛。

当前短时模拟赛以交互体验优先，桌面数据面使用固定 EIP 裸 HTTP 直连；这是一项明确的临时取舍，不应扩展为长期公网服务。OJ、管理后台、座位查询和网页递交仍走 HTTPS。共享座位桥接网络必须同时满足 `Internal=true` 和 `enable_icc=false`，否则不同学生容器之间仍可互访。

## 2. SSH 主机指纹

从云控制台或可信的云助手通道核对比赛服务器 ED25519 主机指纹，并写入 `CONTEST_SSH_HOST_KEY_SHA256`。节省停机后公网 IP 可能改变，编排服务会以实例主机指纹为信任锚，不依赖 IP。不要为了绕过公网 IP 变化而关闭严格校验。

```bash
ssh-keyscan -t ed25519 <比赛服务器IP> > known_hosts
ssh-keygen -lf known_hosts -E sha256
```

`known_hosts` 仍保留为未使用指纹模式时的兼容方案；Docker Compose 挂载的文件可以先建为空文件。指纹模式开启后，以固定的 SHA256 指纹为准。

## 3. 阿里云 RAM 最小权限

编排服务需要查询、启动和停止指定 ECS，查询安全组、规则和 ENI，并授权/撤销临时学生规则。复制 `aliyun-orchestrator-ram-policy.example.json`，替换实例和安全组 ID，并把条件中示例的 `0.0.0.0/0` 替换为与 `source_cidr` 完全一致的单一学生 CIDR，然后创建独立运行时 RAM 用户。阿里云当前的 `AuthorizeSecurityGroup` 不支持按安全组 ARN 限权，`Resource` 必须为 `*`；示例按官方推荐对任一非 TCP、任一非当前学生 CIDR，以及缺失这两个条件键的授权显式 Deny。`RevokeSecurityGroup` 才按目标安全组 ARN 限定，但官方也不能再把撤销权限限制到特定 description、owner 或 rule ID；运行时凭据理论上可以撤掉目标组内静态 OJ TCP 22/80。运行时代码只按稳定 owner 与规则 ID 撤自管规则，这不是 RAM 层强制边界。由于官方还没有目标安全组或目标端口授权条件键，该凭据仍理论上能在其他安全组授权相同来源的任意 TCP 端口；必须独立保管、仅供这一服务使用并定期轮换。静态 OJ `/32` 规则用运维账号一次性建立，不给运行时凭据新增 OJ 规则的权限。

编排器只将完整 description owner 标识（前缀 + `instance=<ECS ID>`）且取得稳定 rule ID 的规则当作自管规则。完全相同的规则重复授权由阿里云自身幂等语义去重；不复用持久 `ClientToken`，避免规则曾撤销后同一场次无法再次发布。撤销每批最多 100 个 ID，不按端口或 CIDR 模糊删除。部署验证完成后应撤销临时部署账号并轮换已暴露的 AccessKey。

## 4. Hydro 插件

宿主机 PM2 部署使用 `deploy/install-hydro-orchestrator-addon.sh`，它会把
`hydro-plugin-orchestrator/` 原子安装到 Hydro 的 addon 目录：

```text
/root/.hydro/addons/orchestrator-submit/index.js
/root/.hydro/addons/orchestrator-submit/package.json
```

Hydro 运行在 Compose 时按 `deploy/hydro-compose-snippet.yml` 挂载 addon、
状态目录和只读 token 文件，不要同时混用两种目录布局。

不要把 token 直接写进 Compose YAML。Hydro 读取权限 `0600` 的 token 文件，并为四类操作配置互相独立、位于持久卷中的状态文件：

```text
ORCHESTRATOR_TOKEN_FILE=/root/.hydro/orchestrator-token
ORCHESTRATOR_IDEMPOTENCY_FILE=/root/.hydro/orchestrator-state/submissions.json
ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE=/root/.hydro/orchestrator-state/notifications.json
ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE=/root/.hydro/orchestrator-state/problem-drafts.json
ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE=/root/.hydro/orchestrator-state/materials.json
ORCHESTRATOR_MATERIAL_MAX_BYTES=201326592
ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS=exam.example.test
ORCHESTRATOR_TEACHER_ADMIN_URL=https://exam.example.test/admin
```

`install-hydro-orchestrator-addon.sh` 和 `health-check.sh` 都从
`/root/.hydro/orchestrator-plugin.env` 读取这四个实际状态文件路径，并逐一要求文件已存在、
权限为 `0600` 且可写；不要再依赖旧的
`/root/.hydro/orchestrator-idempotency.json` 默认路径。

allowlist 是逗号分隔的精确 DNS 主机名，不带 `https://`、端口、路径或通配符。它必须与编排器 `hydro.notify_allowed_https_hosts` 一致；否则 T-5 座位通知会失败关闭，而不是把密码发往未知站点。PM2 部署必须用 `restart hydrooj --update-env` 后 `pm2 save --force`，且只能运行一个 Hydro 应用进程。Compose 部署按 `deploy/hydro-compose-snippet.yml` 建立持久状态卷和只读 token 文件。

插件提供四个仅本机调用的端点：实时/最终送评、Hydro 原生结构化座位通知、比赛私有文件 I/O 题预检与克隆，以及经哈希绑定的比赛 PDF/辅助自测数据发布。材料端点只管理固定的两个私有附件名；只有 OJ 回执与桌面材料字节完全一致时，编排器才会批准材料并允许备赛。站内消息不是 Markdown；插件用 Hydro 的 rich-text 参数生成安全链接。用真实测试比赛、已报名测试用户、比赛内题目进行联调；返回 `rid` 只表示 Hydro 已创建记录并将其送入判题队列，不表示评测完成，必须继续确认该 RID 到达终态且比赛状态已更新。

安装或升级插件后，在 OJ 服务器执行以下命令，把回传接口从公网隐藏；脚本会先备份 Caddyfile，校验并热重载，失败时自动回滚：

```bash
sudo CADDYFILE=/root/.hydro/Caddyfile \
  HYDRO_DOMAIN=oj.example.test \
  CONFIRM_HARDEN_SUBMIT_ROUTE=YES \
  bash /opt/noi-linux-contest-system/deploy/harden-hydro-submit-route.sh
```

将示例域名替换为本校 Hydro 的真实 DNS 名。脚本不提供现场 profile 或域名默认值；完成时必须显示 `public=404 local=403`：公网 `https://oj.example.test/orchestrator/submit` 被隐藏，而本机接口仍存在并拒绝无令牌请求。

## 5. 编排服务与稳定域名

### 5.1 通用宿主机安装器

公开仓库不携带现场 profile。先把源码复制到 root 创建的临时 staging 目录，再用 `mktemp` 创建一次性秘密输入文件；不要使用固定文件名，也不要把 AccessKey 写入命令行或 shell 历史：

```bash
stage=$(sudo mktemp -d /tmp/noi-deploy.XXXXXX)
sudo cp -a /path/to/noi-linux-contest-system/. "${stage}/"
secret_input=$(sudo mktemp /tmp/noi-deploy-secrets.XXXXXX)
sudo chmod 0600 "${secret_input}"
sudoedit "${secret_input}"
```

秘密文件至少填写云 API 凭据；若启用 AI 材料，再填写 AI key：

```dotenv
ALIYUN_ACCESS_KEY_ID=replace-me
ALIYUN_ACCESS_KEY_SECRET=replace-me
NOI_ARTIFACT_AI_API_KEY=replace-me-if-enabled
```

保存后先确认它是 root:root、`0600`、单硬链普通文件，再显式传入全部现场标识。下列域名和 IP 属于文档保留示例，云资源 ID 与指纹占位符也必须替换：

```bash
sudo env \
  NOI_SECRET_INPUT="${secret_input}" \
  NOI_FRONTEND_DOMAIN=exam.example.test \
  NOI_HYDRO_PUBLIC_BASE_URL=https://oj.example.test \
  NOI_GATEWAY_PUBLIC_BASE_URL=http://203.0.113.20 \
  NOI_CONTEST_SOURCE_CIDR=192.0.2.10/32 \
  NOI_ALIYUN_REGION_ID='<区域ID>' \
  NOI_ALIYUN_INSTANCE_ID='<比赛ECS实例ID>' \
  NOI_ALIYUN_DESKTOP_SECURITY_GROUP_ID='<专用安全组ID>' \
  NOI_STUDENT_DESKTOP_SOURCE_CIDR=198.51.100.0/24 \
  NOI_CONTEST_SSH_HOST_KEY_SHA256='<可信通道核对的SHA256指纹>' \
  bash "${stage}/deploy/install-hydro-host.sh" "${stage}"
```

安装器拒绝符号链接、非 root 所有、非 `0600`、多硬链、空文件或不符合 `/tmp/noi-deploy-secrets.XXXXXX` 形状的输入，并在进入正式安装流程后无论成功或失败都删除该文件。旧的现场专用安装入口在公开树中只会拒绝执行。

### 5.2 手工 Compose 启动

```bash
cd orchestrator
cp config.example.yaml config.yaml
cp .env.example .env
mkdir -p data runtime ../artifact-tools
# 编辑 config.yaml 与 .env
docker compose up -d --build
curl http://127.0.0.1:8600/healthz
```

固定 EIP 直连是显式 opt-in；`config.example.yaml` 默认保持 `enabled: false`，避免旧部署被意外切换。首次启用前填写 `.env` 中的真实安全组、EIP、学生来源和 OJ `/32`，再把配置改为：

```yaml
cloud:
  aliyun:
    desktop_access:
      enabled: true
      security_group_id: "${ALIYUN_DESKTOP_SECURITY_GROUP_ID}"
      source_cidr: "${STUDENT_DESKTOP_SOURCE_CIDR}"
      management_source_cidrs:
        - "${CONTEST_SOURCE_CIDR}"
      port: 80
      priority: 20
      description_prefix: "NOI-DESKTOP-DIRECT-MANAGED"
      reconcile_seconds: 5
contest_server:
  gateway_listen: 80
  # 专用 ECS 使用 0.0.0.0；多网卡资格实验室可绑定隔离比赛网卡。
  gateway_bind_address: "${GATEWAY_BIND_ADDRESS:-0.0.0.0}"
  gateway_scheme: http
  gateway_public_base_url: "${GATEWAY_PUBLIC_BASE_URL:-}"
  no_vnc_quality: 9
  no_vnc_compression: 2
```

比赛 ECS 必须使用一张专用、非服务托管的普通（`normal/basic`）安全组，且只有目标 ECS 主网卡使用它：目标 ECS 不能再绑其他安全组，也不能挂任何辅助 ENI（即使辅助 ENI 使用另一安全组）；该组不能共享给其他 ECS 或辅助 ENI。入站基线必须恰好是一个 OJ IPv4 `/32` 的精确 TCP `22/22`、`80/80` 两条 Accept；任何其他规则（包括 Drop、ALL、数字协议、宽端口、地址簿/端口簿、安全组引用或重复规则）都会被视为漂移并阻断开放。编排器会分别按安全组和目标实例分页查询 ECS、ENI 及全部入站规则，不接受局部快照。

ECS 必须绑定固定 EIP。`GATEWAY_PUBLIC_BASE_URL` 可留空以读取云 API 当前 EIP，也可固定为 `http://<EIP>` 或 `http://<EIP>:80`，不允许其他端口；配置的 IP 与云 API 返回值不一致时备赛失败。`orchestrator.public_base_url` 必须是 Hydro 通知 allowlist 中的 HTTPS 根域名：消息先访问 `/desktop/<token>` 校验 `ready`、未截止和座位已发放，再 302 到 EIP；桌面数据不会经 OJ。

公开 TCP/80 的期望生命周期如下。静态 OJ `/32` TCP 22/80 不属于这一状态机，始终保留。

| 比赛状态 | 学生 TCP/80 规则 |
| --- | --- |
| 未登记、`registered`、`preparing` | 关闭；预热探针经 OJ `/32` 管理规则完成 |
| 唯一一场 `ready` 且当前时间早于冻结的 `endAt` | 开启，来源为 `source_cidr` |
| `collecting`、`done`、`error`、手动关机或没有唯一有效 `ready` 场次 | 撤销并持续对账 |

编排器启动时立即恢复对账，之后默认每 5 秒检查一次；授权前、返回开放成功前都重查截止时间。撤销按规则 ID 幂等执行，即使安全组已从 ECS 解绑或出现其他拓扑漂移，也会先直接从配置的安全组回收自管规则，再报告拓扑错误。备赛在公开前先验证有效座位页面 HTTP 200 和作用域 WebSocket 101；`/healthz.desktop_access` 的期望与实际不一致时整体返回 503。截止点由比赛 ECS 上的 systemd timer 精确暂停座位，安全组撤销只证明“新入站已关闭”；云安全组是有状态的，已有 WebSocket 可能最长维持约 910 秒，不能把删除规则当作文件截止证明。

不要直接把 `desktop_access.enabled` 从 `true` 改为 `false`，也不要直接切换到 proxy 后才清理。禁用后的云适配器会跳过 direct 撤销，若此前异常退出可能留下的受管规则就不能自动自愈。切换前必须保持旧 direct 配置，先在教师后台停止专用比赛服务器，确认云状态为 `Stopped`，再确认目标安全组中 description owner 前缀为 `NOI-DESKTOP-DIRECT-MANAGED instance=<比赛ECS-ID>` 的规则数为 0；完成这两个条件后才能修改配置并重启编排器。

`../artifact-tools` 会只读挂载为容器内 `/opt/noi-artifact-tools`。启用 AI 材料前，为每个题目 slug 放置并配置可信 validator 和 oracle；两者都必须是这个目录下的绝对可执行文件。`config.yaml` 只能配置 `api_key_env: NOI_ARTIFACT_AI_API_KEY`，实际 key 只写入权限 `0600` 的 `.env`，不能写入 YAML、命令行、镜像或生成产物。缺少 key、validator、oracle 或只读挂载时，材料生成必须失败关闭。

重复运行宿主机安装脚本时，现有 `config.yaml` 会先进入本次备份并在 staging 复制后原样恢复；安装器只原子更新 `hydro.notify_allowed_https_hosts`。不要用新的 `config.example.yaml` 覆盖现有配置，否则逐题 validator/oracle 和其他现场参数会丢失。

安装器更新 `caddy-exam.conf` 时会先生成候选文件。若主 Caddyfile 已导入该片段，安装器会保存旧片段、校验包含 Hydro 普通站点在内的完整 Caddyfile，再通过 Caddy `/load` 原子热加载；任一步失败都会恢复旧片段并重新加载旧的完整配置。这样不会只改磁盘却继续让内存中的旧桌面反代返回 502。首次安装尚未加入 import 时，安装器只校验“主配置 + 候选 import”并暂存片段，仍须运行 `configure-hydro-caddy.sh` 明确启用站点。

健康检查会先用本机 Caddy 二进制完整校验主 Caddyfile，再把同一文件原文提交给当前 Caddy 管理 API 的 `POST /adapt`，将其 `result` 与 `GET /config/` 返回的活动配置做规范化指纹比较。指纹不能使用 CLI `caddy adapt`：CLI 会把 `file_server hide` 等相对值额外展开成绝对路径，而 `/load` 保存的是管理 API 适配器的原始结果，直接比较会产生假差异。若管理 API 的同源适配结果仍与活动配置不同，健康检查才报告 `Caddy live/disk drift detected`。不要直接重启 Hydro 或覆盖普通 OJ 路由，应先做只读结构差异检查；考试路由另由磁盘片段、ECS 状态和公网 HTTP 探针联合核验。

启用实时评测时，`healthz` 还会检查后台 outbox worker；线程退出或最近一次 SQLite/投递循环异常尚未恢复时会返回 503。不要只检查网页首页为 200。

`orchestrator.realtime_judge_lease_seconds` 默认 45 秒，用于后台送评 worker 的故障恢复租约；应长于单次 Hydro HTTP 请求的最长时间。`orchestrator.realtime_judge_idle_seconds` 默认 0.5 秒，是空队列轮询间隔。正常场景保留默认值，不要用极短轮询制造 OJ 和 SQLite 负载。

当前短时比赛可把 `gateway_public_base_url` 留空，让高速入口直接使用比赛 ECS EIP，以排除香港到杭州中转造成的桌面卡顿。不要手工常开学生 TCP/80；由 `desktop_access` 在 `ready` 窗口授权并在收卷、关机、错误和截止路径撤销。Hydro 消息先进入 HTTPS 启动页，学生默认选择高速直连；OJ Caddy 的 `/s/*` 仅作为明确标注“较慢”的兼容入口，并与直连入口使用同一截止、收卷和失败关闭生命周期。长期产品化时应在 EIP 本机终止 HTTPS。

因此启用 `desktop_access.enabled: true` 时，`frontend_proxy.provider` 必须是 `caddy`，不能使用 `none`/空实现。编排器需要实际写入、热加载并由健康检查证明：唯一未截止的 `ready` 场次中 OJ 兼容入口页面 200、WebSocket 101；其他状态下 OJ `/s/*` 为 503。无法确认关闭时会撤销学生规则并停止专用比赛 VM，不会动普通 OJ。阿里云停机是异步操作，安全兜底必须继续轮询到实例确认为 `Stopped`；仅收到停机请求或看到 `Stopping` 不能当作关闭成功，120 秒仍未完成会明确报错。

学生必须使用编排服务生成的完整座位链接。链接中的 `path=s/<token>/websockify` 指定座位专属 WebSocket，`reconnect=true&reconnect_delay=5000` 用于短暂断线后自动恢复；转发、打印或手工复制时不得截掉查询参数。

V1 不提供提交模式选择，所有比赛固定使用逐题规则：程序回收网页选择 `.cpp` 后立即送入 OJ；该题只要有网页记录，截止就采用最后一次网页版本并禁止目录覆盖。整场没有网页记录的题目，才从 `考号/题目名/题目名.cpp` 创建目录兜底提交。

`seat_pool_maximum` 限制正式人数，`seat_pool_total_maximum` 限制正式加备用的总容器数。容量由 OJ 实时报名驱动，备用位为至少 2 个或报名人数的 10%（向上取整）。系统先运行第一座教师测试，再逐座创建和验收；新报名自动增加座位，不需要老师手工扩容。默认到 T-5 才开放入口并通过 OJ 原生结构化消息发送，T-5 前学生查询不到密码。

单座位故障时使用“替换故障座位”：先暂停旧容器取得一致答案快照，再复制到已验收备用位并原子换绑；网关或数据库提交失败会恢复旧网关和原容器，或保持两边都不可写并明确报错。不要用重新备赛代替故障替换。

AI 材料模式分两次教师授权：首先预检当前比赛题目，非文件读写题只克隆为本场私有题，绝不原地修改共享题；教师批准克隆计划后，系统才更新本场映射。Hydro 只返回正式输入的规范化 SHA256，正式输入内容不发送给 AI。随后 AI 起草 CSP 风格 Markdown/PDF 与 2～4 组自测输入；本地逐题检查其不与正式输入及样例 hash 重复、通过可信 validator，并用可信 oracle 独立运行两次生成一致 `.out`。教师必须预览 PDF、manifest 和 validation report 后再批准发布；批准前不能预热座位。

登记会保存 OJ 的 `beginAt`、`endAt` 和 `rule` 快照；备赛前再次读取并严格比对。`ready` 期间 OJ 时间是唯一权威：延后时原子更新座位池和比赛机 timer，提前结束时立即收卷。备赛成功后，比赛 ECS 会安装持久化的 `noi-contest-freeze-<tid>.timer`，即使 OJ 控制面轮询延迟或 ECS 中途重启，也会在最新确认的截止点立即暂停座位；首次演练必须用 `systemctl status` 和实际到点冻结验证该 timer。

北京式网页入口只负责回收源程序。选手必须在 NOI Linux 编辑器里保留、编译和自测代码，不应把唯一副本直接写在网页中。

学生页面只展示“等待送评/已送入 OJ”等传输状态，不展示判题结果。教师使用比赛创建者或具有查看隐藏比赛成绩权限的账号访问 `https://<OJ域名>/contest/<tid>/scoreboard?realtime=1`；不要把这个教师监控链接及权限账号发给学生。

备赛失败默认会移除本场容器和网关并停止按量实例；收卷失败会保留座位数据，后台可点击“收卷 / 失败重试”，编排服务会重新开机取回文件。不要在收卷错误且座位表仍存在时重新备赛；检测到本轮已有网页递交时，服务也会拒绝该操作以防误删 outbox。

当前 Hydro 插件的递交、通知、题目克隆幂等映射均以权限 `0600` 的原子文件保存，部署时必须保持单个 Hydro 应用进程并持久化三个文件。它们可覆盖普通超时、重试和进程重启；但 `RecordModel.add` 成功后、首次递交幂等落盘前的极端崩溃窗口尚不能与 Mongo 原子提交。演练应故障注入并核对 RID 数量，正式长期方案应改用数据库唯一键收据。

## 6. 首次演练

赛前先在 OJ 服务器运行只读检查，必须达到 `fail=0`：

```bash
sudo EXAM_URL=https://exam.example.test \
  HYDRO_URL=https://oj.example.test \
  EXPECTED_OJ_CIDR=192.0.2.10/32 \
  DESKTOP_ACCESS_MODE=direct \
  STUDENT_CIDRS=198.51.100.0/24 \
  DESKTOP_PROBE_BASE_URL=http://203.0.113.20 \
  bash /opt/noi-linux-contest-system/deploy/health-check.sh
```

以上地址属于文档保留示例网段，必须全部换成部署现场值。健康检查不会猜测任何学校域名、OJ 来源或比赛机地址；漏填即退出。

教师实际操作顺序、收卷核对和故障重试见 `deploy/CONTEST_RUNBOOK.md`。

1. Hydro 创建 OI 赛制测试比赛，加入 1 道题；先不要让测试账号报名，验证空名单也可按最大人数和备用数预热。
2. 后台登记 `文件名=Hydro题号`。人工模式上传 PDF；AI 模式走完私有克隆预检、正式输入 hash、validator/oracle、报告预览和教师批准，确认批准前无法预热。
3. 两个账号随后报名；确认只绑定既有已验收座位，T-5 前无密码、T-5 时收到可点击的 Hydro 原生消息且消息中没有裸 Markdown。
4. 确认两个座位能登录且容器网络 `Internal=true`，正式目录均为 `考号/题目名/题目名.cpp`。
5. 桌面打开 PDF 与辅助数据；对第一题网页递交两次，确认产生不同 RID、最后一次有效，同一请求重试不重复建记录，再修改目录并确认截止不覆盖网页版本。第二题整场不网页递交，验证截止只创建一次目录兜底记录。
6. 一个账号正确提交；另一个故意漏写 freopen。教师账号在实时监控页确认每次递交实时从 Waiting 到终态。
7. 在比赛仍进行时，用真实普通学生账号尝试查看提交记录、分数和排行榜，确认 OI 盲测生效；不能使用管理员账号代替这项验证。
8. 增加一个座位并模拟一个桌面故障，验证已有映射不变、故障位暂停后切到备用位；再故障注入一次数据库提交失败，确认旧网关和旧桌面恢复。
9. 到点或提前结束，确认严格截止后不能再产生有效提交、最后相同源码复用既有 RID、不同最终源码只补交一次，再检查答案报告、选择凭据、送评日志、OJ 记录和排行榜。
10. 确认错误提交为 0 分，比赛服务器进入停机不收费状态。

正式办赛前必须完整通过以上流程。
