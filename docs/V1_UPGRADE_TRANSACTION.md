# V1 已有站点组合升级事务

本流程只适用于已经运行一个 `noi-orchestrator`、Hydro/PM2/Caddy 均在受支持
profile 内、且云资源完全关闭的站点。它不是空机器首次安装流程。

升级始终分成三个不同的信任对象：

1. `noictl install --plan` 输出的公开 `plan_id`，不含现场秘密；
2. `build_v1_install_backup.py` 生成并封存的完整旧组合；
3. `build_v1_private_upgrade_plan.py` 原子发布的 root-only 私有计划及其 SHA256。

缺少任意一项都不能执行。不要手工创建或编辑私有 JSON。

## 1. 公开计划

在目标 Linux 主机的 root-only 会话中，先按正常运行容器的环境提供配置引用，再执行：

```bash
python3 scripts/noictl.py --config /opt/noi/orchestrator/config.yaml \
  install --plan \
  --candidate /root/noi-delivery/candidate \
  --expected-manifest-sha256 '<外部可信manifest摘要>' \
  --json
```

生产模式会从候选归档内运行冻结的资格验证器；资格报告未完整通过时拒绝生成生产计划。
隔离资格机可加 `--qualification-lab`，但该计划不能带到生产主机。

## 2. 封存旧组合

把上一步 `plan_id` 原样交给备份收集器。路径必须按本机实际 root-only 部署填写：

```bash
python3 scripts/build_v1_install_backup.py \
  --output-directory "/root/noi-v1/backup-<plan_id>" \
  --plan-id '<plan_id>' \
  --source-revision '<候选40位revision>' \
  --candidate-manifest-sha256 '<外部可信manifest摘要>' \
  --source-pointer /opt/noi-linux-contest-system/current-source \
  --project-config /opt/noi/orchestrator/config.yaml \
  --project-env /opt/noi/orchestrator/.env \
  --database /opt/noi/orchestrator/data/orchestrator.db \
  --caddyfile /root/.hydro/Caddyfile \
  --snippet /opt/noi/orchestrator/runtime/caddy-exam.conf \
  --oj-origin https://oj.example.test \
  --pm2-bin /root/.nix-profile/bin/pm2
```

标准输出只给出 `backup_manifest_sha256` 等非秘密身份。备份目录必须一直保留到升级和独立复核完成。

PM2 采集器支持 Hydro 的 root-owned Nix profile 入口（包括上例的符号链接），执行前同时校验入口
与解析目标的所有目录。标准 `/nix/store` 的 root-owned `1775` 目录是唯一可写目录例外：sticky bit
保护其中已有的 root-owned 软件包；软件包、可执行文件和其他目录仍不得由普通用户修改。不要为通过
采集而修改 Nix store 权限或关闭路径检查。查询使用 `jlist --silent`，不需要执行 `pm2 update`。

若报告 `PM2 live Hydro environment differs from persistent dump`，这是另一项真实的配置差异，
不是路径兼容问题。必须先由维护人员核对当前运行配置与持久启动配置，确定应保留哪一份，再开始升级。
不要通过忽略差异、盲目 `pm2 save --force` 或重启所有进程来通过检查；非 NOI 配置不在本插件的自动修复范围。

## 3. 私有计划

私有生成器会再次核对公开计划 ID、候选、资格报告中的控制器 image ID、封存备份、
运行容器、配置有效值和当前固定文件；然后在同一父目录的隐藏暂存目录中构建全部输入，
最后一次原子重命名发布。断电留下的固定暂存目录只会在结构和权限完全匹配时自动清理。

```bash
python3 scripts/build_v1_private_upgrade_plan.py \
  --plan-id '<plan_id>' \
  --candidate /root/noi-delivery/candidate \
  --expected-manifest-sha256 '<外部可信manifest摘要>' \
  --controller-image-id 'sha256:<资格报告中的控制器镜像>' \
  --backup-directory "/root/noi-v1/backup-<plan_id>" \
  --backup-manifest-sha256 '<备份收集器输出摘要>' \
  --output-directory "/root/noi-v1/private-<plan_id>" \
  --install-root /opt/noi-linux-contest-system \
  --project-config /opt/noi/orchestrator/config.yaml \
  --project-env /opt/noi/orchestrator/.env \
  --database /opt/noi/orchestrator/data/orchestrator.db \
  --caddyfile /root/.hydro/Caddyfile \
  --snippet /opt/noi/orchestrator/runtime/caddy-exam.conf \
  --python-bin /usr/bin/python3 \
  --bash-bin /bin/bash \
  --pm2-bin /root/.nix-profile/bin/pm2 \
  --node-bin /root/.nix-profile/bin/node
```

从标准输出独立复制 `private_plan` 和 `private_plan_sha256`，不要把目录内的配置、环境或
控制器定义交给 AI、聊天工具或普通用户。

## 4. 唯一执行入口

```bash
python3 scripts/noictl.py install --apply \
  --private-plan '/root/noi-v1/private-<plan_id>/private-upgrade-plan.json' \
  --expected-plan-sha256 '<private_plan_sha256>' \
  --json
```

执行顺序固定为：源码发布、控制器静默、Hydro 集成、关闭态 Caddy、控制器替换、组合终验。
回滚顺序故意不是简单倒序：先停新控制器，再恢复 Hydro，随后恢复 Caddy 和旧控制器，最后
从保留的不可变候选源码执行完整回滚终验。普通 OJ 进程身份、首页、登录和准备服务必须前后相同。

只有两种可接受终态：

- `committed`：新组合运行、入口关闭、云资源关闭、队列为空、普通 OJ 未变化；
- `rollback_verified`：旧组合逐项恢复且通过同样强度的终验，命令退出非零提醒升级未完成。

若终端断开、主机掉电或命令超时，没有第三条人工捷径。恢复电源后必须原样重跑同一个
`install --apply` 命令；持久日志会拒绝继续向前，并自动收敛到完整回滚。不要先手工启动容器、
运行 `pm2 save`、重载 Caddy、替换配置或删除备份。
