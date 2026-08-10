# NOI Linux 模拟赛教师操作手册

本手册是公开发行版模板。文中的 `*.example.test` 与 RFC 5737 地址只用于说明格式；每所学校必须在私有配置中填写自己的 Hydro、教师后台、OJ 来源 CIDR、比赛 EIP 和云资源标识，不得把某一现场 profile 提交到公开仓库。

## 1. 正式比赛前一次性处理

1. 轮换曾经在聊天、终端或截图里出现过的阿里云 RAM AccessKey 和服务器口令。
2. 将新 RAM AccessKey 写入 OJ 服务器的 `/opt/noi-linux-contest-system/orchestrator/.env`，不要写入仓库、操作手册或群消息。
3. 按 `aliyun-orchestrator-ram-policy.example.json` 核对独立运行时 RAM 用户：Start/Stop 只限指定比赛 ECS，Revoke 只限目标安全组；受阿里云 API 权限模型限制，Describe 与 Authorize 的 `Resource` 为 `*`，Authorize 再由显式 Deny 限为 TCP 和当前单一学生 CIDR。官方没有目标安全组或目标端口条件键，仍存在“在其他安全组授权相同来源任意 TCP 端口”的残余边界，因此该 AccessKey 只能供本服务使用并定期轮换。静态 OJ `/32` TCP 22/80 规则仍由运维账号一次性建立。
4. 在 Hydro 创建 OI 赛制比赛，设置准确的开始、结束时间和题目。
5. 统计最大参赛人数并预留 1～2 个故障备用位。建议学生提前报名，但空名单也可以先按容量预热；报名只负责把用户绑定到已经验收的座位。
6. 在一次完整演练中验证名单、登录、桌面、编译、两种递交方式、实时评测、OI 盲测、收卷和关机。盲测必须用真实普通学生账号验证，不能用管理员账号代替。

正式比赛建议选择 `both`：网页递交作为正式版本，文件夹作为备份。网络条件很差时可选 `folder`；只模拟北京网页递交时可选 `web`。

## 2. 题目与文件名映射

人工材料模式下，后台的题目映射每行一题：

```text
sum=P1001
tree=P1002
```

左侧是选手文件名，必须匹配 `^[a-z][a-z0-9_]{0,63}$`；右侧是 Hydro 题号。上例要求选手使用：

```text
选手编号/sum/sum.cpp
选手编号/tree/tree.cpp
```

采用文件输入输出时，程序必须包含对应的 `freopen("sum.in", ...)` 和 `freopen("sum.out", ...)`。系统会对缺文件、题目名不匹配和不符合文件 I/O 规则的代码执行强制零分提交；不要只看收卷文件是否存在，还要在 Hydro 中确认评测记录。

## 3. 赛前 30 分钟

在 OJ 服务器以 root 运行只读健康检查。当前短时比赛采用 EIP 直连；`STUDENT_CIDRS` 必须与编排配置完全一致，`DESKTOP_PROBE_TOKEN` 使用一枚本场已验收座位 token：

```bash
cd /opt/noi-linux-contest-system
sudo EXAM_URL='https://exam.example.test' \
  HYDRO_URL='https://oj.example.test' \
  EXPECTED_OJ_CIDR='192.0.2.10/32' \
  DESKTOP_ACCESS_MODE=direct \
  STUDENT_CIDRS='198.51.100.0/24' \
  DESKTOP_PROBE_BASE_URL='http://203.0.113.20' \
  DESKTOP_PROBE_TOKEN='<已验收座位token>' \
  bash deploy/health-check.sh
```

先把所有示例值替换为现场值。学生来源必须尽量收窄为现场的单一 CIDR；若确需 `0.0.0.0/0`，健康检查还要求显式设置 `CONFIRM_PUBLIC_DESKTOP_CIDR=YES`，并应记录批准人和开放时段。`DESKTOP_PROBE_BASE_URL` 不能填域名中转或其他主机：脚本会从 `cloud_admin.py status` 读取当前固定 EIP，并强制探针根地址恰好是 `http://<该EIP>` 或显式 `:80`。

比赛 ECS 必须只绑定一张专用普通 basic 安全组，不得挂任何辅助 ENI；该组也不得被其他 ECS 或辅助 ENI 使用。关闭态的入站规则集必须恰好是单一 OJ IPv4 `/32` 的精确 TCP `22/22`、`80/80` 两条 Accept；不能存在其他 Accept/Drop、ALL、数字协议、宽端口、地址簿/端口簿、安全组引用或重复规则。比赛尚未 `ready` 时受管学生规则应不存在，此时可以暂不填有效 token，但脚本仍核对关闭状态。比赛 `ready` 后必须同时验证：静态 OJ `/32` TCP 22/80 保留、仅多出一条带完整 owner/instance 标识的学生 TCP/80、有效 EIP 页面返回 200、作用域 WebSocket 返回 101、未知 token 返回 404。

必须满足最终显示 `fail=0`。direct 模式在唯一未截止的 `ready` 场次中必须同时验证：EIP 高速入口页面 200/WS 101，以及 OJ 域名兼容入口页面 200/WS 101；非 `ready`、比赛结束、错误或 ECS `Stopped` 时，OJ `/s/*` 必须恢复 503，学生 TCP/80 规则必须已撤销，EIP 探针应无法建立新连接并显示 000。任何安全组生命周期、EIP 钉定、双链路页面或 WS 验证失败都先解决再开赛。停机兜底必须等阿里云实例真实进入 `Stopped`，`Stopping` 只表示异步请求处理中。安全组是有状态的，撤销后已有 WebSocket 可能短时延续；到点文件截止必须另查 ECS systemd freeze timer 证据。

健康检查使用当前 Caddy 管理 API 的 `POST /adapt` 适配磁盘配置，再与 `GET /config/` 比较；不要用 CLI `caddy adapt` 的结果替代，因为它会把部分相对路径展开成绝对路径并产生假差异。若同源适配后仍报告 `Caddy live/disk drift detected`，先做只读结构差异检查，不要反复重载或重启 Hydro。磁盘片段写着 503 而桌面入口返回 502 时，可以判定考试路由仍在使用旧反代；此时重新运行当前环境的安装器或 `configure-hydro-caddy.sh`，由脚本完成完整校验和原子热加载。

然后打开本校教师后台（例如 `https://exam.example.test/admin`），依次完成：

1. 填写 Hydro 比赛 ID、递交模式、最大参赛人数和备用座位。人工材料模式须显式填写题目映射；AI 材料模式可留空，由系统按 Hydro 比赛题目自动生成稳定映射，但教师必须在生成前核对后台展示的每道题 `slug.in / slug.out`。正式人数不得超过 `seat_pool_maximum`，正式加备用不得超过 `seat_pool_total_maximum`；备用位只用于故障接管，不提高报名上限。
2. 选择材料模式：

   - **AI 生成**：先查看文件 I/O 预检。系统不会修改共享题；需要修改的题会先生成本场私有克隆计划，教师明确批准后才替换本场映射。预检只从 Hydro 取得正式输入的规范化 SHA256，隐藏测试数据内容不会发送给 AI。随后 AI 仅起草 CSP 风格题面和每题 2～4 组梯度自测输入；每组必须避开正式输入和样例 hash、通过逐题可信 validator，`.out` 由本地可信 oracle 独立运行两次且一致后生成。预览 PDF、manifest、validation report，确认题名、文件名、范围、样例、自测梯度和校验结果后，教师再点击批准。缺 AI key、validator、oracle、正式输入 hash 或任一校验失败时不得强行开赛。
   - **人工上传**：显式填写题目映射并上传合并后的本场试题 PDF；如需本地自测数据，再上传 ZIP。人工材料同样要先批准冻结后才能预热。

   人工 ZIP 根目录下每道题一个英文目录，目录名必须与题目映射左侧一致，例如：

   ```text
   sum/1.in
   sum/1.ans
   sum/2.in
   sum/2.ans
   tree/1.in
   tree/1.out
   ```

   系统也接受外面多包一层总文件夹。每道题至少要有一个 `.in` 文件；未知题目目录、路径越界、加密条目或特殊文件都会被拒绝。该 ZIP 只用于学生桌面自测，不收卷、不评测、不计分。
3. 材料显示“已批准并冻结”后点击“提前预热全部座位”，等待 `最大人数 + 备用座位` 全部逐个验收成功；即使此时 Hydro 还没有学生报名也可以进行。不要重复点击。
   登记后不要再修改 Hydro 的开始时间、截止时间或赛制；如确需修改，先在 Hydro 改好，再回本页重新登记后备赛。系统会在开云机前比对快照，不一致会拒绝备赛。
4. 学生报名后点击“同步报名名单”或等待自动同步。该操作只绑定已验收座位，不创建或重建已有容器；已有学生的 `uid → slot` 保持不变。默认在比赛开始前 5 分钟才释放登录信息。
5. T-5 用一个测试账号检查 Hydro 站内消息：应显示可点击的桌面链接、准考证号和密码，不能显示 `##`、`[文字](URL)` 等裸 Markdown。消息链接先访问 HTTPS 控制面 `/desktop/<token>`，校验后显示“高速直连（推荐）”与“兼容入口（较慢）”两个按钮；高速入口应进入 `http://<比赛EIP>/s/<token>/vnc.html?...quality=9&compression=2...`，后续 WebSocket 不再经过 OJ。浏览器对 HTTP 的临时安全提示属于当前方案的已知取舍。兼容入口必须使用相同 token 正常进入 HTTPS OJ 代理。T-5 前学生查询页面不应暴露密码；未送达时检查通知队列、持久状态文件、HTTPS allowlist 和编排器 `public_base_url`，不要改用群发明文密码。
6. 导出已发放座位 CSV，保存到仅教师可访问的位置；发给学生时必须保留座位链接的完整查询参数，不要截断 `?path=...&reconnect=...`。
7. 随机抽查至少一个座位：登录 noVNC，确认是官方 NOI Linux GNOME 桌面；打开桌面“试题”检查 PDF；如有自测数据，再检查桌面“测试数据”按题分目录且只读；最后用 Code::Blocks 或 Geany 编译一个程序。
8. `web` 或 `both` 模式还要从桌面打开“CSP 程序回收系统”，为每道题选择 `.cpp` 文件，重复提交并核对最后时间、大小、查看内容和“已送入 OJ”状态。该状态表示 RID 已创建并入队，不表示判题完成。
9. 使用教师账号打开 `https://oj.example.test/contest/<tid>/scoreboard?realtime=1`（替换为本校域名），确认能看到实时提交；再用真实普通学生账号确认比赛中看不到提交结果、分数和排行榜。
10. 确认已绑定人数等于 Hydro 已报名人数、所有已绑定座位来自已验收池，再发放信息。
11. 在比赛 ECS 检查 `systemctl status noi-contest-freeze-<tid>.timer` 为 active，触发时间等于 Hydro 截止时间；首次演练必须实际等到触发并确认所有座位先变为 `paused`、nginx 随后停止（新连接和已有 noVNC WebSocket 均断开），不能只看 timer 文件存在。先冻结是为了不让 nginx 的优雅停机等待拖延文件截止；若停 nginx 失败，systemd 会在座位保持暂停的情况下重试。OJ/编排器失联时阿里云规则可能要等控制面恢复后才能撤销，但比赛 ECS 本地 TCP/80 数据面必须仍在截止点关闭，SSH 保留用于收卷。

座位 CSV 和后台口令都属于敏感信息，不要投屏、发到学生群或放进公共网盘。

## 4. 学生登录与比赛中操作

学生使用教师发放的座位入口进入桌面。站内消息的 HTTPS 地址只做一次校验和跳转，浏览器最终地址应是比赛 EIP；页面与 WebSocket 不经 OJ。教师只需监看后台状态，不应在比赛中重新“备赛”、替换试题或修改题目映射。

- `web`：每次新的明确递交都会立即创建独立 Hydro 记录并进入判题队列；同一题可以多次递交，以最后一次为准，同一次网络重试不会重复建记录。
- `folder`：没有点击递交事件；系统在截止时冻结桌面，从官方目录结构收卷后才创建 Hydro 记录并评测。
- `both`：网页递交实时送评；收卷时复用最后一次网页提交的 RID，某题没有网页版本时才把文件夹版本作为最终记录送评。文件夹内容仍完整留档。

教师在后台点击“OJ 实时监控”，或直接打开 `https://oj.example.test/contest/<tid>/scoreboard?realtime=1`（替换为本校域名）。该入口需要比赛创建者或“查看隐藏比赛成绩”权限。Hydro OI 赛制下，普通学生在比赛中仍应保持盲测；学生程序回收页面只显示源码保存和送评状态，不显示状态、分数、测试点或排行榜。

学生页面显示“已送入 OJ”或后台取得 RID，只能证明记录已创建并入队。教师应在实时监控页继续确认它从 Waiting 到达最终状态；积压时不要反复点击收卷或让学生重复同一次提交。

赛中临时增加人数时，只使用“现场扩容”：填写新增正式座位/备用位，刷新到当前座位池 revision 后明确勾选教师确认。系统只创建新增槽位，全部验收后才发布，原学生链接和 `uid → slot` 不变；若任一新增座位失败，确认后台仍显示原池正常，不要转而点击重新备赛。

个别桌面故障时，只使用“替换故障座位”：选择座位并填写原因，教师确认后系统先暂停旧容器取得一致的答案快照，再复制到已验收备用位、撤销旧 token 并重新发放凭据。若网关或数据库提交失败，旧网关和原容器会恢复；先确认学生仍可进入旧座位再重试。不要在旧容器仍运行写入时手工复制答案目录。

建议赛中至少记录：异常发生时间、座位号、学生账号、浏览器提示、教师采取的操作。不要记录学生密码。

## 5. 截止与收卷

备赛时写入比赛 ECS 的持久化 systemd timer 会在 Hydro 截止点先暂停全部座位、再停止本机 nginx 桌面入口，因此 OJ 控制面的自动收卷轮询即使稍晚或暂时失联，也不会保留可操作桌面或接收晚写文件；之后系统自动收卷，也可以由教师在后台手动触发。手动操作时：

1. 确认比赛确实结束，再点击“收卷”。
2. 等待后台状态变为 `done`；收卷期间不要重新备赛。
3. 检查本场数据目录中的 `folder_report.json`、`web_report.json`、`selection.json`、`report.json` 和 `submit_log.json`。
4. 在 Hydro 确认每位选手、每道题都有预期的最终评测结果，并抽查排名和分数。网页版本应复用赛中已经创建的 RID，不应因收卷再多出一条重复记录；`folder` 或目录回退版本除外。
5. 重新运行 direct 模式健康检查，确认比赛 ECS 为 `Stopped`、受管学生 TCP/80 已撤销且 EIP 桌面探针为 000；静态 OJ `/32` TCP 22/80 仍应存在。
6. 将收卷目录和座位 CSV 加密备份，按学校的数据保存期限处理。

RID 只代表已创建记录并进入队列；只有该 RID 在 Hydro 中出现最终评测结果，才算完成回传。后台的“已收卷”不能替代 Hydro 核对。

网页递交以服务端接收并持久化的 `accepted_at_ms` 对照选手个人有效比赛窗口；截止时刻本身不再接受新提交。截止前已经可靠入队的请求可以在截止后完成送评，网络重试继续返回原 RID；截止后新创建的请求必须被拒绝。文件夹模式由比赛 ECS 上的持久化 timer 先冻结、再关闭本地 nginx 并打包，两条路径都不能靠手工延长页面时间绕过截止。云安全组是有状态的，撤销规则后已有连接可能短时保留；控制面失联时规则本身也可能延后到恢复后撤销，因此精确截止证据必须来自 ECS timer 已暂停座位且停止 nginx，而不是仅看到 TCP/80 规则消失。

## 6. 故障处理

### 备赛失败

系统会清理本场容器、撤下桌面网关并尝试停止比赛 ECS。记录后台错误，修正名单、题号、云权限或配置后再重新备赛。先用健康检查确认没有遗留运行中的比赛机。

### 收卷失败

不要重新备赛。座位、实时送评 outbox 和答案数据会保留，直接点击“收卷/失败重试”；系统会在需要时重新启动比赛 ECS 取回文件。网络或云 API 恢复后可重复收卷，直到状态为 `done`。检测到本轮已有网页递交时，编排服务会拒绝重新备赛以避免删除记录。

### 实时送评健康检查失败

`/healthz` 返回 503 或学生长时间停在“等待送评”时，先停止改配置，检查编排日志、SQLite 文件权限、Hydro 内网端点和两端 token 是否一致。认证错误会保留在 outbox 中自动重试，修复后应使用原幂等键恢复；不要让学生为了“刷新”而重复创建新提交。教师同时在 OJ 实时监控页核对 RID 是否已经存在。

### 某题显示缺文件或强制零分

依次核对文件名、目录层级、扩展名、题目映射和 `freopen`。保留原始收卷文件，不要手工覆盖后再假装自动收卷成功；如需人工裁决，应另写有时间和操作人的记录。

### AI 材料生成被阻断

按后台报告逐项检查：本场是否已批准私有克隆计划、每题是否取得正式输入 hash、`/opt/noi-artifact-tools` 是否只读挂载、题目 slug 是否各自配置 validator/oracle、AI key 是否只在编排 `.env` 中。validator 拒绝、oracle 两次输出不同、练习输入与正式/样例 hash 重复都属于必须阻断，不能跳过报告直接把草稿发给学生。时间紧时切换到经过人工核对的 PDF/ZIP 模式，而不是降低校验。

### T-5 消息没有链接或显示 Markdown

Hydro 消息气泡不解析通用 Markdown。确认部署的是带 `/orchestrator/submit/notify` 的插件，`ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE` 位于持久目录且权限为 `0600`，插件 `ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS` 与编排器 allowlist 完全一致，`orchestrator.public_base_url` 也是该 HTTPS 根域名。不要把裸 HTTP EIP 直接塞进插件消息，也不要把 Markdown 字符串作为普通私信补发；编排器会生成 HTTPS 一跳入口并在校验后跳往 EIP。

### 桌面返回 403

先确认浏览器最终地址是否为 `http://<比赛EIP>/s/<token>/...`。若仍停留在 `https://exam...`，检查 `/desktop/<token>` 是否因比赛非 `ready`、已截止、存在多个 `ready` 场次或座位尚未 `released` 而失败关闭。若已经到 EIP，检查 token 对应的比赛 ECS nginx 动态配置、有效页面 200 和 WS 101，不要恢复跨地域代理。

只有执行旧代理回退时，才检查 `/opt/noi-linux-contest-system/orchestrator/runtime/caddy-exam.conf` 的桌面上游是否含：

```caddy
header_up Host {upstream_hostport}
```

再核对 Caddy 配置和比赛 ECS 公网 IP。direct 模式中，唯一未截止的 `ready` 场次应同时提供 EIP 高速入口和 OJ HTTPS 兼容入口；健康检查必须分别证明有效页面 200、WebSocket 101。场次非 `ready`、截止、错误或 ECS 停止时，OJ `/s/*` 必须恢复 503，学生 TCP/80 规则必须撤销。兼容入口只作应急，不应作为全班默认链接，否则会重新引入跨地域中转延迟。

### 学生 TCP/80 未按生命周期关闭

先看 `http://127.0.0.1:8600/healthz` 的 `desktop_access.desired_open`、`open` 和 `healthy`，再查编排日志中的 reconcile/revoke 错误。启动恢复和默认 5 秒对账会重试；收卷、手动关机和错误路径若无法确认撤销会停止比赛 ECS 并报错。紧急情况下只在云控制台删除 description 以 `NOI-DESKTOP-DIRECT-MANAGED instance=<比赛ECS-ID> ` 开头的学生规则，绝不能删除或改写 OJ `/32` 的 TCP 22/80 管理与回退规则。随后保留故障记录并修复 RAM 权限或规则冲突，不能把手工删除当作系统已恢复。

若要停用 direct 或切回 proxy，必须在仍使用旧 direct 配置时先停止比赛 ECS，确认实例为 `Stopped` 且上述受管学生规则为 0，然后才能改 `desktop_access.enabled` 并重启。不能先禁用再等系统自动清理；禁用后的适配器不会管理旧 direct 规则。

### 比赛结束后 ECS 仍在运行

先在后台重试收卷或停止。仍未停止时，在 OJ 服务器执行：

```bash
docker exec noi-orchestrator python cloud_admin.py status
docker exec noi-orchestrator python cloud_admin.py stop
docker exec noi-orchestrator python cloud_admin.py status
```

最终应为 `Stopped`，并使用 StopCharging。若云 API 失败，立即在阿里云控制台手动“节省停机/停止不收费”，随后保存故障记录。

## 7. 每场比赛留档清单

- Hydro 比赛 ID、开始/结束时间、题目映射和递交模式。
- 最终参赛人数及座位 CSV（受限保存）。
- 五份收卷/提交报告和原始答案目录。
- Hydro 评测 rid 核对结果与最终排名快照。
- 赛前、赛后健康检查输出（输出中不得含口令或 AccessKey）。
- 异常记录、人工处理记录和负责人。

比赛负责人：__________　技术负责人：__________　紧急联系电话：__________
