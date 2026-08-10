# 使用 AI 分析脱敏故障

AI 排障只允许使用公开代码、公开文档和 `noictl support-bundle` 生成的脱敏支持包。当前支持包是**单个 JSON 文件**，不是压缩包；管理员应先打开复核，再通过私密渠道提供，不要直接上传到公开平台。

生成命令会在终端 JSON 的 `actions[].sha256` 中返回支持文件摘要。请把这段命令输出与支持文件分开保存，并在交给 AI 前自行复算、比对 SHA256。支持文件本身不会保存自己的摘要，也不会读取 Git 元数据或重新读取 `noictl` 脚本；公开 tag/commit 必须由管理员另行提供。

## 不要提供给 AI

- `.env` 或 `config.yaml` 原文；
- AccessKey、密码、Token、Cookie、私钥或 `known_hosts`；
- 完整桌面 URL、gateway token、VNC 密码或准考证号；
- 学生源码、答案目录、隐藏数据、Mongo/SQLite 备份；
- 未脱敏的 Caddy、Hydro、Docker、云 API 或访问日志。

## 建议提示词

```text
你正在分析 Hydro NOI Contest Kit 的单文件脱敏 JSON 支持包。

- 只使用支持文件中的顶层 schema_version、bundle_type、runtime、configuration、diagnostics.doctor、collection、redactions，以及管理员另行提供的公开 tag/commit 和已核对 SHA256。
- 先确认外部 SHA256 已由管理员核对，再检查 schema_version、bundle_type 和 diagnostics.doctor 中的 profile 证据；公开 tag/commit 缺失或不匹配就停止。不要声称支持文件内含 Git revision、manifest 或独立 doctor.json。
- 不尝试还原任何摘要、匿名标识或已删字段。
- 不索取原始配置、日志、数据库、学生源码或秘密。
- 把结论分成：已证明事实、合理推断、尚缺证据。
- 优先保护普通 Hydro OJ；不要把重启 Hydro/Caddy、重新备赛或删库作为常规修复。
- 所有写操作先给出影响、备份、回滚和停止条件；没有正式 noictl plan_id 时只提供人工审阅建议，不执行。
- 容量与性能结论只能用于支持包记录的精确镜像 digest、实例规格、人数和网络条件。
```

## 适合 AI 的问题

- 某个稳定检查码表示什么；
- 哪些证据支持“客户端问题”“比赛机问题”或“控制面问题”；
- 哪份公开文档包含对应恢复流程；
- 需要补采哪些只读指标；
- 如何把现场时间线整理成可复核的故障报告。

## 不适合 AI 单独决定的问题

- 是否在活动比赛中重启服务；
- 是否修改成绩或重新生成 RID；
- 是否删除孤儿容器、答案目录或云安全组规则；
- 是否跳过镜像、SSH 指纹、截止计时器或容量门禁；
- 是否把单座成功外推为正式人数容量。
