# 本地离线镜像包

本目录中的清单格式和脚本用于把已经验收的 NOI Linux 正式桌面镜像交给另一台 Linux Docker 主机。镜像包只写入仓库根目录下被 Git 忽略的 `local-release/`，或者显式指定的仓库外目录；不得把镜像归档提交到 Git。

## 安全边界

- 本流程不构建镜像，只导出调用时已经存在的一个固定标签。
- 标签必须显式带版本，不能使用 `latest` 或 registry digest。正式环境使用 `noi-linux-official:2.0`。
- 导出前强制验证 `org.noi.desktop.contract=finalizer-status-v1`，要求 `org.noi.iso.sha256` 是 64 位小写十六进制 SHA256，并要求 `org.opencontainers.image.revision` 与 `--source-revision` 指定的 40 位小写 commit 完全一致。缺少该 OCI revision label 的旧镜像必须按目标 commit 重建，不能靠导出参数补写来源。
- 清单固定镜像标签、不可变 Docker image ID、全部镜像标签、归档字节数和归档 SHA256。
- 包内 `SHA256SUMS` 同时覆盖镜像归档、manifest、Schema 和导入脚本；公开 Release 清单分别固定包内 `manifest.json` 与原始 `SHA256SUMS` 的 SHA256。导入器拒绝符号链接和清单外条目，先把经逐文件摘要验证的字节复制到权限收紧的私有临时快照，再访问 Docker daemon。
- 导出者必须显式填写构建该桌面镜像的 40 位 Git commit；不能把当前目录 HEAD 猜成镜像来源。
- 导入先验证清单、归档 SHA256/大小以及 Docker archive 内部的标签和 image ID，然后才调用 `docker image load`；导入后再次验证标签、image ID 和全部 labels。
- SHA256 只能发现传输损坏或与可信清单不一致，不能单独证明发布者身份。必须从项目的正式 GitHub Release 取得公开 `release-manifest.json`，并从该 Release 对应的固定 tag/40 位 commit 运行仓库内导入器；私发镜像包不能替代这个信任起点。
- 导入期间不要并发执行 `docker tag`、`docker image load` 或 `docker image rm`。

## 依赖

两端均需 Bash、Docker CLI/daemon、Python 3、GNU `tar` 和 GNU coreutils（脚本使用 `sha256sum`、`stat -c`、`mv -T` 及 GNU 风格的 `--`）。基线是 Ubuntu/Debian 类 Linux，并要求 Python/内核提供 `O_NOFOLLOW` 与 `O_DIRECTORY`；不把 BusyBox/Alpine 工具链视为已验证环境。选择 `zstd` 压缩时，两端还需安装 `zstd`。运行用户必须有访问 Docker daemon 的权限。

## 导出

从仓库根目录执行：

```bash
bash scripts/export-local-image-bundle.sh \
  --image noi-linux-official:2.0 \
  --source-revision 0123456789abcdef0123456789abcdef01234567 \
  --compression zstd
```

未指定 `--bundle-dir` 时，包会写到 `local-release/noi-linux-official_2.0/`。目标目录必须不存在，脚本不会覆盖旧包。可显式指定 U 盘或其他仓库外目录：

```bash
bash scripts/export-local-image-bundle.sh \
  --image noi-linux-official:2.0 \
  --source-revision 0123456789abcdef0123456789abcdef01234567 \
  --compression none \
  --bundle-dir /mnt/usb/noi-linux-official-2.0
```

`--compression none` 生成 `.tar`，`--compression zstd` 生成 `.tar.zst`。压缩模式会先生成并检查临时 tar，再压缩；磁盘需同时容纳未压缩和压缩文件。成功目录只包含：

```text
manifest.json
local-image-bundle-manifest.schema.json
noi-linux-official_2.0.tar[.zst]
SHA256SUMS
import-local-image-bundle.sh
```

导出成功后保存终端显示的源码 commit、镜像 ID、归档 SHA256、`Manifest SHA256` 和 `Checksums SHA256`。把后两项分别填写到公开 Release 清单的 `bundle_manifest_sha256` 与 `bundle_checksums_sha256`，再发布 Release；接收方会强制核对。`source_revision` 不是任意声明：导出器还会核对镜像既有的 `org.opencontainers.image.revision`，不一致即停止。

脚本使用 `umask 077`，通常产生仅导出用户可读的 `0700` 目录和 `0600` 文件。如果通过 U 盘交给目标机上的另一个账号，应在可信目标机上把整个包 `chown` 给实际运行 Docker 导入命令的账号，或只为该账号补充读取权限；不要直接把包改成全员可写。

## 导入

先从项目正式 GitHub Release 下载 `release-manifest.json`，并把公开仓库检出到该 Release 声明的完整 `release.git_revision`（固定 tag 也必须解析到这个 commit）。接着在目标 Linux Docker 主机上复制整个私发包目录。**不要先执行包内脚本**；从可信固定 commit 的仓库路径运行导入器，并同时传入公开清单：

```bash
test "$(git -C /opt/hydro-noi-contest-kit rev-parse HEAD)" = \
  '<release.git_revision 中的 40 位 commit>'
bash /opt/hydro-noi-contest-kit/scripts/import-local-image-bundle.sh \
  --bundle-dir /mnt/usb/noi-linux-official-2.0 \
  --release-manifest /opt/releases/0.1.0-alpha.1/release-manifest.json
```

包内的 `import-local-image-bundle.sh` 是被校验的交付内容之一，可用于审计或与可信仓库版本比对，但不能作为最初的信任入口。

导入器用 no-follow、regular-file 和有界读取规则取得外部 Release 清单。随后前后两次枚举包的物理目录，要求恰好五项：归档、`manifest.json`、Schema、导入器和 `SHA256SUMS`；校验表本身只列出前四项。它先用 Release 清单固定原始 `manifest.json` 和原始 `SHA256SUMS` 字节，再用该校验表固定其余内容，并在私有临时目录形成快照。Release 与 bundle 的源码 revision、固定非 `latest` 镜像 tag、image ID、desktop contract 和 ISO SHA256 任一不一致，或出现符号链接、额外条目、文件缺失及内容变化，都会在第一次 Docker daemon 调用前停止。后续解析和 `docker image load` 只读取快照，避免校验后原包被替换。导入期间临时目录还需容纳一份完整归档，成功或失败退出时会自动删除。

如果目标标签不存在，脚本加载并验证镜像。如果同一标签已经指向清单中的 image ID，脚本验证后直接成功，不重复加载。

如果同一标签指向其他 image ID，脚本默认停止，不会覆盖。确认当前没有比赛座位依赖旧镜像，并已记录旧 image ID 后，才可显式切换：

```bash
bash /opt/hydro-noi-contest-kit/scripts/import-local-image-bundle.sh \
  --bundle-dir /mnt/usb/noi-linux-official-2.0 \
  --release-manifest /opt/releases/0.1.0-alpha.1/release-manifest.json \
  --replace-existing
```

`--replace-existing` 在加载或验收失败时把原标签恢复到旧 image ID。它不会删除旧镜像；验收完成前不要 prune，以便人工回滚：

```bash
docker image tag '<旧的 sha256:image-id>' noi-linux-official:2.0
```

脚本成功只表示离线包完整且 Docker 中的固定标签、ID、labels 一致；正式启用前仍须按部署验收流程检查运行时健康、容量和座位回收。

## 清单格式

版本 1 的 JSON Schema 位于 `release/local-image-bundle-manifest.schema.json`，合法样例位于 `release/local-image-bundle-manifest.example.json`。导出包会携带 Schema 副本。导入脚本内置同等的关键字段和语义校验，因此目标机不需要额外安装 Python `jsonschema` 包。
