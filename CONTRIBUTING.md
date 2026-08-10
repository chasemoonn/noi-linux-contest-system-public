# 贡献指南

感谢帮助完善 Hydro NOI Contest Kit。这个项目服务于有明确截止时间和成绩记录的模拟赛，因此“代码能运行”不是充分条件；每个改动还需要说明失败方式、回滚方式和对普通 Hydro OJ 的影响。

参与前请同时遵守 [社区行为规范](CODE_OF_CONDUCT.md)。安全问题不要创建公开 Issue，请按 [安全策略](SECURITY.md) 私下报告。

## 开始之前

1. 阅读 `docs/PRODUCT.md` 与 `docs/SUPPORT_MATRIX.md`。
2. 确认改动属于正式支持 profile、实验适配器或纯文档。
3. 不要把真实站点配置、密码、Token、IP、实例 ID、主机指纹、学生数据或镜像归档加入仓库。
4. 不要在活动比赛或正式服务器上首次验证新功能。

## 本地检查

```bash
python -m compileall -q orchestrator
(cd orchestrator && python -m unittest discover -s tests -v)
node --check hydro-plugin-orchestrator/index.js
node --test hydro-plugin-orchestrator/tests/*.test.js
bash -n deploy/*.sh scripts/*.sh
python scripts/build_demo.py --check
python scripts/check_public_release.py
git diff --check
```

涉及桌面镜像、云生命周期、Hydro 插件或截止收卷的改动还必须提供隔离 canary 证据。涉及容量声明的改动必须使用当前正式桌面镜像重新完成对应并发测试，不能复用旧镜像数据。

## Pull Request 要求

PR 需要说明：

- 改了什么，以及为什么；
- 对学生、教师和站点管理员分别有什么影响；
- 失败时系统处于什么状态；
- 如何回滚；
- 运行了哪些自动测试与真人验证；
- 是否改变配置 schema、数据库、插件状态或发布物格式；
- 是否需要更新支持矩阵、Release Notes 或离线镜像 manifest。

## 设计原则

- 保持一人一容器、OI 盲测、截止先冻结和普通 OJ 隔离。
- `doctor` 和默认诊断必须严格只读。
- 所有高风险操作都应支持计划预览、显式执行和失败回滚。
- AI 只能生成草稿和解释诊断，不能成为安全门或唯一真值来源。
- 新云厂商只有通过完整能力认证后才能标记为正式支持。
