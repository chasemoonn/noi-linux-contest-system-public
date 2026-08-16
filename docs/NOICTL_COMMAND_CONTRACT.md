# `noictl` 命令契约

`noictl` 是统一安装、诊断、升级和支持入口。只读命令、已有站点的 V1 组合升级事务与首次
干净安装事务已经实现；比赛事务和卸载仍按本页状态区分，不能把尚未交付的命令当成可用功能。

## 核心原则

1. `doctor` 默认严格只读，不开机、不改安全组、不写配置、不重启服务。
2. 所有高风险操作先生成计划；执行必须显式引用仍然有效的 `plan_id`。
3. 人类输出默认中文，自动化和 AI 使用 `--json`；二者表达相同事实。
4. 输出不得包含密码、Token、AccessKey、Cookie、私钥、学生源码或完整桌面入口。
5. 活动比赛是安装、升级、卸载和配置迁移的硬阻断。
6. 普通 Hydro OJ 首页、登录和评测链路是所有写事务的前后门禁。
7. 失败时优先恢复上一个完整版本组合，不能只回滚某一个容器。

## 命令面

| 命令 | 当前状态 | 默认是否只读 | 说明 |
| --- | --- | --- | --- |
| `noictl doctor` | 已实现（静态） | 是 | 第一批只做本机平台、配置和支持 profile 声明的静态检查；Hydro、云、比赛机和网络探测尚未实现 |
| `noictl init` | 后续契约 | 否 | 交互生成版本化站点配置和秘密引用，不安装服务 |
| `noictl config validate` | 已实现 | 是 | 校验环境引用和现有 orchestrator 配置不变量；不连接外部系统 |
| `noictl config show --effective --redact` | 已实现 | 是 | 显示环境引用展开后的脱敏配置及每个已显示值的来源 |
| `noictl install --plan` | 已实现 | 是 | 要求候选目录外的可信 manifest SHA；再用候选归档内验证器重验全部签名资格证据，输出目标、备份、验证及回滚计划 |
| `noictl install --apply --private-plan PATH --expected-plan-sha256 HEX64` | 已实现（升级与首次干净安装） | 否 | 按私有计划的精确 `operation` 分派六阶段组合事务；任一失败自动完整回滚，禁止手工切换执行器 |
| `noictl verify` | 后续契约 | 是 | 运行安装后静态检查，不创建真实座位 |
| `noictl canary --plan` | 后续契约 | 是 | 展示单座 canary 将创建的资源、费用和持续时间 |
| `noictl canary --apply --plan-id ID` | 后续契约 | 否 | 创建一座受控真人 canary，并在结束后收卷和关闭 |
| `noictl contest preflight TID` | 后续契约 | 是 | 检查指定比赛的 OI、时间、题目、材料、容量和入口条件 |
| `noictl upgrade VERSION --plan` | 后续契约 | 是 | 检查兼容矩阵、迁移、备份和回滚点 |
| `noictl upgrade VERSION --apply --plan-id ID` | 后续契约 | 否 | 切换完整发布组合，失败自动回滚 |
| `noictl rollback --plan` | 后续契约 | 是 | 显示将恢复的版本组合和可能丢失的运行时变化 |
| `noictl rollback --apply --plan-id ID` | 后续契约 | 否 | 恢复匹配的镜像、插件、配置、数据库和 Caddy 快照 |
| `noictl support-bundle` | 已实现 | 是 | 生成一个脱敏、带哈希且不覆盖现有文件的本地 JSON 诊断包 |
| `noictl uninstall --keep-data --plan` | 后续契约 | 是 | 显示卸载服务但保留数据的动作 |

## 可执行面

从仓库根目录使用 Python 3 运行；Linux 与 Windows 均可执行：

```bash
python scripts/noictl.py --config orchestrator/config.yaml config validate --json
python scripts/noictl.py --config orchestrator/config.yaml doctor
python scripts/noictl.py --config orchestrator/config.yaml doctor --json
python scripts/noictl.py --config orchestrator/config.yaml \
  config show --effective --redact --json
python scripts/noictl.py --config orchestrator/config.yaml \
  support-bundle --output noictl-support-local.json --json
```

仓库不会附带真实 `orchestrator/config.yaml`。首次运行前，应从 `orchestrator/config.example.yaml` 创建一份仅管理员可读、不会提交的站点配置，或把 `--config` 指向仓库外的私有配置；先在当前安全会话中提供模板要求的环境引用，再按上面的顺序运行 `config validate` 和 `doctor`。默认阿里云模板当前至少要求 `ADMIN_PASSWORD`、`HYDRO_ORCHESTRATOR_TOKEN`、`STUDENT_DESKTOP_SOURCE_CIDR`、`ALIYUN_ACCESS_KEY_ID`、`ALIYUN_ACCESS_KEY_SECRET`。只允许把变量名和脱敏 JSON 提供给 AI，不要提供变量值。完整步骤见 [使用 AI 协助安装](ai/INSTALL_WITH_AI.md)。

`--json` 和 `--config PATH` 可以放在主命令前，也可以放在最终子命令后。配置路径按以下顺序选择：

1. 显式 `--config PATH`；
2. `ORCHESTRATOR_CONFIG` 环境变量；
3. 当前目录的 `config.yaml`；
4. 仓库中的 `orchestrator/config.yaml`。

`install --apply` 不读取 `--config`，也不接受候选目录或资格实验室开关。它只接受
`build_v1_private_upgrade_plan.py` 或 `build_v1_private_clean_install_plan.py` 原子发布的 root-only
私有计划，以及从计划生成器标准输出中独立复制的 SHA256。私有计划必须先绑定公开计划 ID、封存备份、资格报告中的控制器
镜像、当前运行容器、配置有效值、Caddy/Hydro/云关闭态和完整回滚组合。执行被中断时，必须
用完全相同的计划与 SHA256 原样重跑，使持久事务进入 `rollback_verified`；禁止改用旧部署脚本。
完整命令顺序见 [V1 已有站点组合升级事务](V1_UPGRADE_TRANSACTION.md)。首次安装的独立基线、
私有计划与回滚语义见 [V1 干净目标首次安装事务](V1_CLEAN_INSTALL_TRANSACTION.md)。两者源码
入口已交付，但都必须等正式资格报告的 Linux/Docker、跨机器、教师安装和容量硬门通过后才可
用于生产。

`config show` 必须同时给出 `--effective` 和 `--redact`，不存在关闭脱敏的参数。第一批的 effective 含义是展开配置文件中的 `${NAME}` 与 `${NAME:-default}`；现有校验器通过 `.get()` 使用但未写入配置对象的隐式默认值不会被虚构到输出中。秘密、AccessKey、Cookie、私钥路径、身份、完整 URL/URI、桌面入口、文件系统路径和网络拓扑值（包括私有镜像名、Docker 网络、端口、区域和主机指纹）会被替换为类别占位符；未知字段的键和值默认一并隐藏。

第一批 `doctor` 有意不导入或调用 HTTP、云 SDK、SSH、Mongo、Docker、systemd、PM2、Caddy 和进程管理接口。显式 UNC/双斜线网络配置路径会在访问前拒绝；挂载成本地盘符或挂载点的远程文件系统无法由 CLI 可靠识别，调用方仍应只传本地路径。配置虽然有效但不匹配 `aliyun-hydro5-pm2-direct-v1` 核心静态声明时退出 `3`。静态通过只表示 CLI 运行平台与配置通过本地校验，不表示 Hydro 正常、云权限正确、比赛机可达、没有活动比赛，也不表示 15+2 容量已经签字。

`support-bundle` 是第一批唯一允许写入的命令。未给 `--output` 时，它在当前目录独占创建 `noictl-support-<UTC时间>.json`；给出 `--output` 时只接受当前目录中的安全 `.json` 文件名，不接受目录、绝对路径、UNC、NTFS ADS、空格或 Windows 保留设备名，且绝不覆盖。POSIX 上以 `0600` 创建。支持包只含：

- 第一批 doctor 的脱敏结果；
- 操作系统与 Python 的非身份运行时元数据；
- noictl 命令契约版本和规范化脱敏有效配置的 SHA256；不二次读取运行中的脚本，也不输出可能含内联秘密的原始配置摘要；
- 明确为零的网络探测、服务命令、日志读取、数据库读取和配置引用的秘密文件读取计数。

显式选择的配置文件本身会通过单个文件描述符有界读取以做静态校验，但配置所引用的文件不会被打开；配置路径的最终组件若为符号链接/重解析点会被拒绝，Windows 上还会在打开后、读取前复核文件身份。校验失败时，不会再次访问、统计大小或散列该输入。它不收集 `.env`、环境变量列表、配置明文、日志、进程列表、主机名、IP、Hydro/Mongo 数据、SSH 私钥、known_hosts、座位、入口、学生身份或源码。显式文件名不会回显到 JSON 结果。支持包生成成功只代表安全收集动作成功；包内 doctor 可以仍有警告或失败诊断。

## 计划与执行

计划必须包含：

- `plan_id` 和创建时间；
- 当前 Git/Release 版本和目标版本；
- 配置 schema、有效配置摘要和秘密引用摘要；
- 活动比赛、队列、Hydro/Caddy/编排器和云状态快照；
- 将创建、修改、重启、停止或删除的精确目标；
- 普通 OJ 预计影响；
- 备份位置和恢复组合；
- 不可逆迁移和人工确认项；
- 计划过期条件。

执行前必须重新读取关键事实。只要出现活动比赛、配置摘要变化、版本变化、云目标变化、SSH 指纹变化或备份不可用，旧 `plan_id` 就失效，必须重新生成计划。

## JSON 输出

所有命令支持 `--json`，顶层统一为：

```json
{
  "schema_version": 1,
  "command": "doctor",
  "status": "ok",
  "changed": false,
  "plan_id": null,
  "summary": "所有只读检查通过",
  "checks": [],
  "actions": [],
  "warnings": [],
  "redactions": ["secrets", "student_identity", "gateway_tokens"]
}
```

每个检查至少包含：

```json
{
  "code": "HYDRO_SINGLE_PROCESS",
  "status": "pass",
  "scope": "hydro",
  "message": "检测到一个 Hydro 应用进程",
  "evidence": {"process_count": 1},
  "remediation": null
}
```

`evidence` 只能包含可安全公开给支持人员的值。敏感值只允许输出“存在/缺失”“匹配/不匹配”和不可逆摘要。

第一批的 `config show --json` 在统一顶层之外增加 `effective_config` 与 `sources`；两者都已经过同一脱敏门禁。`support-bundle --json` 只在 `actions` 中报告安全目标类别和文件 SHA256：默认文件可以报告由程序生成的安全文件名，用户显式指定的文件名只报告为 `user-specified-local-file`。

只要参数中出现独立的 `--json`，参数解析失败也返回同一 JSON 顶层，不打印 argparse usage 或原始参数；检查码为 `CLI_ARGUMENTS_VALID`，退出码为 `2`。由于参数尚未形成有效命令，此时 `command` 固定为 `unknown`。

程序入口会将可配置的标准输出/错误流设为 UTF-8 且使用安全的编码回退；JSON 文本仅使用 ASCII 字符和 Unicode 转义。即使输出被重定向到严格 ASCII 流，也不应因中文诊断产生 traceback。

## 稳定退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功，所有硬门通过 |
| `2` | 命令参数或配置 schema 无效 |
| `3` | 当前环境不属于所选支持 profile |
| `4` | 只读门禁失败，未执行写操作 |
| `5` | `plan_id` 过期或现场状态已经改变 |
| `6` | 执行失败，但已成功恢复上一个完整版本组合 |
| `7` | 执行和自动恢复均未完成，需要人工接管 |
| `8` | 存在活动比赛或未完成收卷，拒绝高风险操作 |
| `9` | 诊断发现秘密、拓扑或证据可能泄露，拒绝生成公开支持包 |

具体失败通过稳定 `checks[].code` 区分，AI 和文档不能依赖易变的自然语言错误文本。

## AI 调用约束

AI 可以：

- 运行只读命令；
- 解释 `--json`；
- 生成 `--plan` 并总结风险；
- 在用户明确批准后调用对应的 `--apply --plan-id`；
- 读取已经脱敏的支持包。

AI 不可以：

- 绕过 `plan_id` 直接调用内部脚本；
- 把 `--apply` 当作只读探测；
- 读取或回显秘密文件；
- 用原始 SSH、云 CLI、Mongo shell 或 SQLite 写命令替代 `noictl` 安全门；
- 在活动比赛期间建议升级或重新安装；
- 将一次成功 canary 外推成未经签字的容量结论。

## 实现顺序

第一批已经实现 `doctor`、`config validate/show`、`support-bundle`；除支持包的单个本地输出文件外全部只读。第二批实现 `init` 和安装计划。源码层已有独立事务原语 `scripts/stage_v1_source_release.py`：它只安装经外部摘要钉死的 production-qualified 源码 release，以持久 pending/committed/rollback receipt 和原子 `current-source` 指针处理崩溃恢复，不触碰服务。只有服务组合的完整备份、回滚和普通 OJ 门禁经过测试后，才把它接入完整安装与升级的 `--apply`。
