# V1 单座真实演练手册

这不是在生产比赛上试功能。它是一场使用专用合成考号、真实控制机、真实桌面机和真实
OJ 记录的资格演练。只有八个阶段全部完成并由离线核验器组合后，单座资格才能从
`pending` 变为 `passed`。

## 1. 安全边界

- 必须使用 `9999` 开头的 12 位专用合成考号，不得使用真实学生。
- 私有上下文、组件事实、阶段观测和证据目录均必须是 root 所有且权限不高于
  `0700/0600`；不得放在 `/tmp`、下载目录或普通用户可换包的路径。
- 三类主机使用同一个已提交、已冻结、跟踪文件无变更的 Git revision。
- 三台主机必须启用同源时钟同步，开始前证明 UTC 偏差不超过 2 秒。
- 采集器不记录学生 UID、用户名、token、Cookie、密码或源码原文。
- 组件事实、普通 OJ 基线与阶段事实之间最多间隔 120 秒。陈旧快照不能复用。
- 任一采集器返回 `NO_GO`、留下 `.pending-*` 目录或发现既有阶段文件时，整个
  session 作废并建立新 session；不要删除旧文件后重放。

## 2. 三机组件冻结

先在三台机器各自建立 root 证据目录：

```bash
sudo install -d -o root -g root -m 0700 /root/noi-v1-rehearsal-input
sudo install -d -o root -g root -m 0700 /root/noi-v1-sessions
```

控制机采集正在运行的编排器不可变镜像 ID：

```bash
sudo python3 scripts/collect_v1_components.py \
  --role control --docker-bin /usr/bin/docker \
  --container noi-orchestrator \
  --output /root/noi-v1-rehearsal-input/control.json
```

桌面机对专用演练座位采集桌面镜像 ID、源码 revision 和
`finalizer-status-v1` 合同：

```bash
sudo python3 scripts/collect_v1_components.py \
  --role desktop --docker-bin /usr/bin/docker \
  --container '<synthetic-seat-container>' \
  --output /root/noi-v1-rehearsal-input/desktop.json
```

OJ 主机对安装器实际部署的 `index.js` 和 `package.json` 两文件树做精确文件集与
字节摘要；源码树中的测试文件由 Linux CI 证明，不得伪装成现场已部署文件：

```bash
sudo python3 scripts/collect_v1_components.py \
  --role oj \
  --plugin-root /root/.hydro/addons/orchestrator-submit \
  --output /root/noi-v1-rehearsal-input/oj.json
```

三份 JSON 必须在 120 秒内通过 root-only 通道汇总到会话初始化机。任一镜像、标签或插件
字节在会话期间变化，后续阶段会拒绝继续。

## 3. 初始化私有会话

私有上下文只存放在 root-only 文件：

```json
{
  "candidate_id": "999900000001",
  "contest_id": "private-synthetic-contest-id",
  "cutoff_at_ms": 1900000000000,
  "problem_slug": "apple",
  "seat_candidate": "CSP001",
  "seat_id": "private-synthetic-seat-id"
}
```

```bash
sudo python3 scripts/init_v1_single_seat_session.py \
  --control-components /root/noi-v1-rehearsal-input/control.json \
  --desktop-components /root/noi-v1-rehearsal-input/desktop.json \
  --oj-components /root/noi-v1-rehearsal-input/oj.json \
  --private-context /root/noi-v1-rehearsal-input/private-context.json \
  --output-parent /root/noi-v1-sessions --name rehearsal-001
```

会话会保存三份原始组件事实及其 SHA256，但只把加盐后的比赛与座位摘要带入阶段事实。
`candidate_id` 是专用合成 OJ 学生号，用来证明没有使用真人；`seat_candidate` 是系统实际
发放并出现在 CSP 答案目录中的准考证号。两者不得混为一个身份。
初始化后将整个会话骨架通过 root-only 通道分发到控制机、桌面机和 OJ 主机。各机只生成
属于自己角色的阶段；完成后把该阶段的 `facts/<phase>.json` 和
`artifacts/<phase>/` 原样收回离线复核机。不合并、不覆盖同名文件，最后按 SHA256 核对后
才组合九份事实。

## 4. 每个阶段的固定手续

下表的角色必须在对应机器上采集。具体字段以
`release/v1-single-seat-phase-fact.schema.json` 和
`scripts/verify_v1_single_seat_evidence.py` 为准。

| 序号 | 阶段 | 角色 | 必须由真实动作产生的观测 |
|---|---|---|---|
| 1 | `materials` | control | PDF 发布/桌面字节一致，2～4 组 `.in/.out`，OJ 发布回执 |
| 2 | `desktop` | desktop | 五项入口、CSP 路径、页面 200、WebSocket 101 |
| 3 | `compile` | desktop | 读取正式源文件，编译成功，指定输入与期望输出摘要一致 |
| 4 | `manual_submit` | control | 手动网页回收生成独立 OJ RID，且已送达 |
| 5 | `cutoff_submit` | control | 到点冻结变更后源码，触发一次补交并得到新 RID |
| 6 | `oj_record` | oj | 两个 RID 分别绑定两个源码摘要，教师与学生视图均可见 |
| 7 | `collection` | control | 自动归档、送达日志、最终 RID/源码和收卷回执绑定，进入 `safe_wait` |
| 8 | `shutdown` | control | 延迟边界后入口关闭、规则为零、座位为零、云主机 `STOPPED` |
| 9 | `test_cleanup` | oj | 仅对本 session 的合成测试赛使用 OJ 原生删除；比赛、报名、讨论、定时任务和比赛关联记录均已清零 |

每阶段开始时：

1. OJ 主机立即生成新基线：

   ```bash
   sudo python3 scripts/collect_v1_ordinary_oj_observation.py \
     --oj-origin https://oj.example.edu \
     --pm2-bin /absolute/root-owned/path/to/pm2 \
     --output /root/noi-v1-rehearsal-input/ordinary-<phase>.json
   ```

   `--pm2-bin` 必须是 `readlink -f` 后的最终 root-owned 可执行文件，不接受 symlink。

2. 对应角色用第 2 节命令重新采集当前组件事实，并与 OJ 基线一起在
   120 秒内传到该角色的 root-only 输入目录。
3. 由该阶段的真实 API/运行时程序生成 `observations.json`和脱敏原始产物。禁止手工填造 RID、
   SHA、HTTP 状态、云状态或收卷回执。
4. 运行固定阶段采集器，例如：

   ```bash
   sudo python3 scripts/collect_v1_single_seat_phase_fact.py \
     --phase materials --role control \
     --session-directory /root/noi-v1-sessions/rehearsal-001 \
     --ordinary-oj /root/noi-v1-rehearsal-input/ordinary-materials.json \
     --observed-components /root/noi-v1-rehearsal-input/control-materials.json \
     --observations /root/noi-v1-rehearsal-input/materials-observations.json \
     --artifact api=/root/noi-v1-rehearsal-input/materials-api-sanitized.json
   ```

采集器会认证 session 中的三份原始组件事实，核对当前角色组件、当前 Git revision、OJ
快照时间和阶段字段，然后以不覆盖方式生成原始产物副本和阶段事实。

## 5. 离线组合与判定

九份事实只能使用仓库内的组合器按固定顺序处理。`shutdown` 完成后先保存全部原始
资格证据，再由有权限的技术管理员在 OJ 比赛编辑页执行原生删除；不得由标题、时间或
“测试”字样推断目标。`test_cleanup` 必须在 `shutdown` 核验后 30 分钟内由 OJ 主机采集：

```bash
sudo install -d -o root -g root -m 0700 /root/noi-v1-rehearsal-input
sudo --preserve-env=HYDRO_MONGO_URI python3 scripts/collect_v1_test_cleanup_observation.py \
  --session /root/noi-v1-sessions/rehearsal-001/session.json \
  --private-context /root/noi-v1-rehearsal-input/private-context.json \
  --domain-id system \
  --output /root/noi-v1-rehearsal-input/test-cleanup-observations.json
```

采集器只读 OJ MongoDB，连续三次核对清理结果；它不提供删除能力，也不声称仅凭最终
数据库状态就能证明管理员具体使用了哪一个删除入口。OJ 原生删除是本流程的操作要求，
Mongo 最终态是独立验收要求。输出只包含加盐后的
比赛摘要、计数和绑定内容的回执摘要。随后按第 4 节的固定手续生成 `test_cleanup` 阶段事实，
不得把 Mongo URI 写入命令行、日志、阶段事实或资格产物。

```bash
revision="$(git rev-parse HEAD)"
python3 scripts/verify_v1_single_seat_evidence.py \
  --materials /root/noi-v1-sessions/rehearsal-001/facts/materials.json \
  --desktop /root/noi-v1-sessions/rehearsal-001/facts/desktop.json \
  --compile /root/noi-v1-sessions/rehearsal-001/facts/compile.json \
  --manual-submit /root/noi-v1-sessions/rehearsal-001/facts/manual_submit.json \
  --cutoff-submit /root/noi-v1-sessions/rehearsal-001/facts/cutoff_submit.json \
  --oj-record /root/noi-v1-sessions/rehearsal-001/facts/oj_record.json \
  --collection /root/noi-v1-sessions/rehearsal-001/facts/collection.json \
  --shutdown /root/noi-v1-sessions/rehearsal-001/facts/shutdown.json \
  --test-cleanup /root/noi-v1-sessions/rehearsal-001/facts/test_cleanup.json \
  --artifact-directory /root/noi-v1-sessions/rehearsal-001/artifacts \
  --expected-revision "${revision}" \
  --output /root/noi-v1-sessions/rehearsal-001/single-seat-evidence.json
```

唯一通过标志是命令退出码为零且组合文件 `status=passed`。不接受截图、人工勾选、老日志或
“功能看起来正常”作为替代。单座通过也不等于已完成两机镜像回滚、六类故障演练或 15+2
一小时容量验收。
