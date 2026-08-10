# 组合发布清单

一次可安装发布不是单个 Git commit 或单个 Docker tag，而是一组必须彼此匹配的组件：

- 公开源码 revision；
- 编排器镜像 digest；
- Hydro 插件文件 SHA256 与兼容版本；
- 私下交付的桌面镜像 bundle manifest SHA256 与原始 `SHA256SUMS` SHA256；
- 配置 schema、支持 profile 和验证证据边界。

`release-manifest.json` 用 `release-manifest.schema.json` 校验，并随 GitHub Release 发布。完整桌面归档不上传 GitHub；公开清单记录离线包 manifest 与原始 `SHA256SUMS` 的 SHA256，以及桌面不可变 image ID，使接收方能确认“公开源码、控制面和私发镜像”属于同一个组合。接收方必须从 Release 对应的可信固定 tag/commit 运行仓库内导入器，并把这份公开清单传给它；不能先执行尚未验证的包内脚本。

## 版本规则

- `version` 使用 SemVer；Alpha 版本带预发布后缀，例如 `0.1.0-alpha.1`。
- `git_revision` 必须是公开仓库中的完整 40 位 commit。
- 所有容器都使用 `sha256:` digest；不能使用 `latest` 作为部署身份。
- Hydro 插件记录实际文件 SHA256，不能只记录 npm 包名。
- 桌面 `image_tag` 必须是显式的非 `latest` 版本标签；桌面条目必须与离线 bundle 内的 source revision、tag、image ID、ISO SHA256 和 contract 一致。
- `bundle_manifest_sha256` 和 `bundle_checksums_sha256` 必须分别取导出器成功输出的 `Manifest SHA256` 与 `Checksums SHA256`；不能手工重排或重写 `SHA256SUMS` 后继续沿用旧摘要。
- 离线镜像的 `org.opencontainers.image.revision` 必须是 40 位小写 commit，并与桌面 `source_revision`、顶层 `release.git_revision` 完全一致。
- 验证记录必须写明 `passed`、`pending` 或 `failed`；不允许省略未通过的容量项。

合法结构示例见 `release-manifest.example.json`。示例中的摘要、域名和版本都是合成值，不能直接用于部署。

## 发布前核对

1. Git 工作树干净，CI 对目标 commit 全绿。
2. 公开发布边界检查通过，没有运行时配置、秘密、数据库或镜像归档。
3. 编排器镜像和插件从目标 commit 构建，摘要记录进清单。
4. 离线桌面包已经由导出脚本生成；两个公开 bundle 摘要已原样写入清单，并已用可信 commit 中的导入器和公开 Release 清单在另一台主机成功导入验证。
5. 单座验收记录适用于同一组件组合。
6. 15+2 未完成时，`capacity_15_plus_2.status` 必须保留为 `pending`。
7. Release Notes 明确升级、回滚、已知限制和安全影响。
