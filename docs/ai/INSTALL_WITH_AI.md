# 使用 AI 协助安装

这份指令用于让 AI 帮助管理员理解环境、整理配置和解释脱敏诊断。它不是“让 AI 自动接管服务器”的脚本。已有站点升级和首次干净安装均提供持久事务化 `--apply`，但只有程序生成、root-only、外部 SHA 钉住的私有计划可以执行；未通过资格演练的候选仍不得用于生产。

## 开始前

管理员应先准备：

- 本仓库的固定 Git tag 或 commit；
- 一台不承载正式比赛的 Hydro 测试主机；
- 一台符合 [支持矩阵](../SUPPORT_MATRIX.md) 的独立比赛 ECS；
- 从可信渠道获得的离线桌面镜像包；
- 自己保管的 `.env`、SSH 私钥、AccessKey 和管理员密码。

不要把 `.env`、`config.yaml`、私钥、AccessKey、Token、真实学生名单、比赛入口或收卷文件上传给 AI。AI 只读取 `noictl --json` 的脱敏输出、公开文档和没有秘密的配置模板。

## 推荐协作流程

1. 从 `orchestrator/config.example.yaml` 复制一份站点配置到仓库外、仅管理员可读的目录；不要直接修改或提交模板。Linux 上应把文件权限收紧到 `0600`，Windows 上应使用仅当前管理员可读的 ACL。
2. 在管理员自己的安全终端中设置模板所需的环境变量。默认阿里云 profile 至少需要以下 5 项；只把变量名留在命令或文档中，不要把值发给 AI：

   - `ADMIN_PASSWORD`：至少 16 个字符；
   - `HYDRO_ORCHESTRATOR_TOKEN`：至少 32 个字符；
   - `STUDENT_DESKTOP_SOURCE_CIDR`：严格的 IPv4 CIDR；
   - `ALIYUN_ACCESS_KEY_ID`：非空；
   - `ALIYUN_ACCESS_KEY_SECRET`：非空。

3. 先验证站点配置，再对同一文件运行 `doctor`：

   ```bash
   python scripts/noictl.py --config /path/to/config.yaml \
     config validate --json
   python scripts/noictl.py --config /path/to/config.yaml \
     doctor --json
   ```

   配置中的 `${NAME}` 引用只从启动 `noictl` 的当前进程环境读取。第一批 CLI 故意不提供 `--env-file`，避免把任意秘密文件变成诊断输入；请由管理员在本机安全会话中预先设置所需环境变量，不要把变量值、shell 历史或环境转储交给 AI。

4. 只把上述 JSON 输出交给 AI；先自行检查其中没有现场标识。若 `error_kind=missing_environment`，先对照模板和上面的变量名在本机补齐，不要把变量值交给 AI。
5. 让 AI 按稳定的 `checks[].code` 解释失败项，并引用仓库中的具体文档。`validation_failed` 表示配置结构或取值不符合当前 schema；它不会回显可能带秘密的原始校验文本，应由管理员在本机逐项对照模板。
6. 每修正一项后重新运行只读命令，不允许 AI 用 SSH、云 CLI、Mongo shell 或 SQLite 写命令替代门禁。
7. 当只读门禁全部通过后，由管理员审阅公开计划、封存备份和私有计划摘要。AI 不得手工拼计划或调用旧部署脚本；只有管理员明确批准同一私有计划后，才可运行 `install --apply`。该命令会按计划内固定的 `upgrade` 或 `clean-install` 自动分流，人工不得选择另一执行器。资格实验室计划永远不能用于生产。

## 可直接复制的提示词

```text
你正在协助安装 Hydro NOI Contest Kit。请严格遵守以下规则：

1. 只依据我提供的仓库版本、公开文档和 noictl 脱敏 JSON 输出工作。
2. 不索取、不读取、不复述 .env、config.yaml 原文、AccessKey、密码、Token、Cookie、SSH 私钥、学生身份、比赛入口、源码、答案或隐藏数据。
3. 不建议直接修改 MongoDB、SQLite、Hydro 比赛数据、Caddy 活动配置或云安全组。
4. 不运行或建议任何会启动收费资源、重启 Hydro/Caddy、创建座位、改变成绩或删除数据的命令。
5. 逐条解释 checks[].code：说明事实、风险、对应文档和最小修复；不要根据自然语言错误猜测。
6. 如果发现活动比赛、版本不明确、镜像摘要不明、SSH 指纹缺失、配置漂移或普通 OJ 健康异常，立即停止并明确写出阻断原因。
7. install --apply 只允许接收程序生成且外部 SHA 钉住的私有升级或干净安装计划。不得手工拼 JSON、替换候选、改用旧脚本、手工切换 operation，或把 qualification-lab 计划用于生产。

先告诉我你识别到的仓库版本、支持 profile 和 noictl 输出 schema_version；信息不足时只列缺失项。
```

## AI 必须停止的情况

- 当前分支、tag、离线镜像 ID 或 manifest 无法对应；
- `doctor` 或 `config validate` 返回非零；
- 检测到活动比赛、未收卷记录或待处理递交/通知；
- 普通 Hydro 首页、登录或评测基线异常；
- 需要读取秘密文件才能继续；
- 操作将修改 Hydro/Caddy、启动云资源或产生费用，但没有程序生成的计划和管理员确认；
- 建议超出 `aliyun-hydro5-pm2-direct-v1` 支持边界。

## 安装后仍需人工完成

- 用普通学生和教师账号做一座完整 canary；
- 检查中文输入、编程字符、PDF、编译、保存、网页递交和目录收卷；
- 实际等到截止，核对先冻结、再收卷、再撤销入口和停机；
- 对照 Hydro RID、收卷报告与源码 SHA256；
- 在正式人数规模上完成独立容量验收。
