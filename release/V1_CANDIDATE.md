# V1 候选冻结与资格边界

V1 候选分成两个明确层级，不能混用措辞：

1. **Source Candidate**：Git 工作树干净，产品合同、公开边界和递交故障注入检查通过，源码归档与逐文件 SHA256 已冻结。它只允许进入独立测试机。
2. **Production Qualified**：同一源码 revision、控制镜像、桌面镜像和 Hydro 插件已经完成 Linux CI、跨机导入/回滚、单座、故障恢复、普通 OJ 隔离和 15+2 一小时验收，并由两名不同复核者签字。

没有资格报告的候选必须写入：

```json
"production_qualified": false
```

不得把 Windows 单元测试、演示页、一次线上比赛或旧镜像的容量记录替代这些门槛。

## 1. 生成源码候选

从干净、已提交的目标分支执行：

```bash
python scripts/build_v1_candidate.py \
  --version 0.2.0-alpha.1 \
  --output-root local-release
```

生成器会：

- 拒绝脏工作树；
- 运行 `check_v1_product_contract.py`；
- 运行 `check_public_release.py`；
- 运行 `run_v1_fault_injection.py`，验证回执丢失、插件/控制器重启、核对网络中断和并发认领；
- 从精确 `HEAD` 的 Git blob 构造平台无关 tar，固定 owner、时间和 `0644/0755`；
- 记录 Git tree、每个普通文件的 mode、长度和 SHA256；
- 拒绝 symlink、gitlink 和其他非普通 Git 条目；
- 重新读取 tar，逐项核对内容与权限；
- 默认标记为未取得生产资格。

生成器会在标准输出给出 manifest SHA256。部署者必须从可信 Release/交付通道取得并核对这个摘要；候选目录内的文件不能自证其来源。

本地产物位于被 Git 忽略的 `local-release/`，不能提交到公开源码树。

## 2. 只读核验候选

```bash
python scripts/verify_v1_candidate.py \
  local-release/noi-v1-0.2.0-alpha.1-<revision>
```

部署到正式环境之前必须追加：

```bash
python scripts/verify_v1_candidate.py \
  local-release/noi-v1-0.2.0-alpha.1-<revision> \
  --require-production-qualified
```

第二条命令在资格报告缺失、待验、失败、哈希不符或组件 revision 不一致时必须退出非零。

## 3. 资格报告

不要手工把资格项改成 `passed`。先由已经通过独立验证的机器证据自动生成当前资格报告：

```bash
python scripts/build_v1_qualification_report.py \
  --linux-ci /root/noi-evidence/v1-linux-ci-evidence.json \
  --linux-ci-log-directory /root/noi-evidence/v1-linux-ci-logs \
  --cross-machine /root/noi-evidence/v1-cross-machine-image-evidence.json \
  --single-seat /root/noi-evidence/v1-single-seat-evidence.json \
  --capacity /root/noi-evidence/capacity-evidence.json \
  --capacity-artifact-root /root/noi-evidence \
  --reviewer '<teacher-reviewer>' --reviewer '<operations-reviewer>' \
  --output /root/noi-evidence/qualification-report.json
```

当前生成器会复核 Linux CI 原始日志、跨机导入回滚、单座、15+2 与普通 OJ 隔离，并把尚未
完成机器证据协议的“独立老师从零安装”和六类故障恢复明确保持为 `pending`。它没有把这些
项目手工设为真的参数；已有站点升级事务虽已交付，但在独立老师从零安装和六类故障演练完成前，
报告必须保持 `production_qualified=false`。
示例 JSON 只用于理解 schema，不能复制后手工填写成生产报告。

随后使用：

```bash
python scripts/verify_v1_qualification.py qualification-report.json
python scripts/verify_v1_qualification.py \
  qualification-report.json \
  --require-production-qualified
```

全部门槛通过后，使用报告重新生成候选：

```bash
python scripts/build_v1_candidate.py \
  --version 0.2.0-alpha.1 \
  --output-root local-release \
  --qualification-report /root/noi-evidence/qualification-report.json \
  --linux-ci-evidence /root/noi-evidence/v1-linux-ci-evidence.json \
  --linux-ci-log-directory /root/noi-evidence/v1-linux-ci-logs \
  --cross-machine-evidence /root/noi-evidence/v1-cross-machine-image-evidence.json \
  --single-seat-evidence /root/noi-evidence/v1-single-seat-evidence.json \
  --capacity-evidence /root/noi-evidence/capacity-evidence.json \
  --capacity-artifact-root /root/noi-evidence
```

报告把 Linux CI 或跨机导入回滚标为 `passed` 时，上述相应输入也是强制项。构建器会重新读取
全部十项 CI 日志，并把 revision/tree 与当前 HEAD 绑定；跨机证据会重新核对两台不同主机、六阶段
顺序、原始事实摘要、最终恢复和精确桌面 image ID。只有报告中的文字或布尔值不能生成合格候选。

当资格报告仍把单座验收标为 `pending` 时不要传 `--single-seat-evidence`。当报告把它标为
`passed` 时，该参数为强制项；报告引用必须精确为 `single-seat-evidence.json`，构建器会核验
其 SHA256、源码 revision、四个组件和九阶段检查，并把组合证据一并封入候选目录。

同理，当报告把 15+2 容量验收标为 `passed` 时，`--capacity-evidence` 为强制项，引用必须
精确为 `capacity-evidence.json`。构建器会复核 SHA256、源码 revision、四个组件以及报告中的
容量摘要，并通过 `--capacity-artifact-root` 当场重读六类原始文件、核对字节数与 SHA256，
再把不含凭据的容量证据封入候选目录。更详细的原始采样仍保存在私有证据目录，不进入公开源码候选。

资格只适用于报告中的精确组件组合。源码、控制镜像、桌面镜像、Hydro 插件、实例规格、网络路径或关键资源限制任一变化，都必须重新验收受影响的门槛。

跨机器桌面镜像资格不能由一次 `docker load` 或一次成功启动替代。必须使用
`deploy/V1_QUALIFICATION.md` 中的六阶段事实协议，证明导出机和导入机不同、提升的是离线包
声明的同一不可变 image ID，并在回滚、再次提升和最终恢复中保持 image/source 成对一致。
