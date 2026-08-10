# 首次公开发布检查单

本页用于把维护者的私有开发仓库发布成真正适合陌生教师查看、安装和复核的公共仓库。`scripts/check_public_release.py` 只检查目标工作树；它不能证明旧 commit、tag、PR、Actions artifact 或 Release 资产已经脱敏。

## 当前历史边界

早期开发提交曾记录维护者现场的域名、公网地址、实例标识和 SSH 主机指纹。审计没有在历史中发现真实 AccessKey、Token 或私钥，但这些拓扑信息仍不应随第一版公共仓库发布。旧提交也包含维护者个人提交邮箱。

因此，首次公开默认采用以下安全边界：

1. 当前仓库继续作为私有开发与现场运维仓库；
2. 只从一个已经通过 CI 和公开发布检查的 commit 导出文件树；
3. 在新的空目录、新 Git 历史和新的公共仓库中创建首次提交；
4. 公开仓库不导入旧 branch、tag、PR、Actions artifact 或 Release；
5. 后续所有公开开发都从该公共基线继续。

不要在没有备份、复核和维护者明确确认时对现有仓库执行 history rewrite 或 force-push。

## 导出原则

- 来源必须是完整 40 位 commit，且该 commit 的工作树已由 CI 验证；
- 使用 `git archive <commit>` 或等价只读方式导出，不复制当前脏工作树；
- 新仓库首次提交使用维护者认可的公开身份和 noreply 邮箱；
- 导出后再次运行完整测试、演示资产检查和 `scripts/check_public_release.py`；
- 再次扫描全部新历史，确认只有一个预期初始提交；
- GitHub 仓库设为 public 前，由第二人复核文件清单和扫描输出。

## GitHub 只包含什么

- 源码、测试、无密钥示例配置和文档；
- 脱敏、可重复生成的演示资产；
- JSON Schema、Release manifest、校验和与导入导出脚本；
- 不含真实桌面镜像、ISO、OCI/Docker archive、运行时数据库、日志、备份、现场配置或秘密。

完整桌面镜像在被 Git 忽略的本地目录生成，通过私下渠道交付；公共 Release manifest 只记录离线包 manifest 的 SHA256、镜像 immutable ID、ISO SHA256、桌面 contract 和源码 revision。

## 首个 Alpha Release 门槛

- [ ] 目标 commit 的 Python、Node、Shell、演示资产和公开发布检查全部通过；
- [ ] README、LICENSE、NOTICE、第三方声明、贡献指南、安全策略和行为规范齐全；
- [ ] 支持范围精确为 Hydro `>=5.0.1,<5.1.0` 与 `aliyun-hydro5-pm2-direct-v1`；
- [ ] `noictl doctor/config/support-bundle` 的只读与脱敏契约通过测试；
- [ ] 离线镜像包在另一台 Linux Docker 主机完成校验导入；
- [ ] 组合 Release manifest 中 15+2 容量状态仍为 `pending`；
- [ ] 新公共仓库历史不包含私有仓库旧 commit 或 tag；
- [ ] 仓库可见性切换前完成第二人复核；
- [ ] 发布后从未登录浏览器重新阅读首页、Release 和安全报告入口。

完成这些门槛只允许发布 Alpha。只有 [开源路线图](OPEN_SOURCE_ROADMAP.md) 的独立单座复现与 15+2 容量阶段分别签字后，才能提升对应成熟度声明。
