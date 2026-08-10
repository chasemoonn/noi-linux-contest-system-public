# AI 协作边界

AI 不是安装前提，也不是比赛安全边界。未来会在本目录提供三类受限指令：

- `INSTALL_WITH_AI.md`：围绕版本化 Release、`noictl doctor --json`、计划预览和人工批准安装；
- `TROUBLESHOOT_WITH_AI.md`：只使用脱敏支持包进行诊断；
- `REPRODUCE_IN_LAB.md`：只在本地或隔离实验环境复现和验收。

当前入口：

- [使用 AI 协助安装](INSTALL_WITH_AI.md)
- [使用 AI 分析脱敏故障](TROUBLESHOOT_WITH_AI.md)
- [在隔离实验环境中复现](REPRODUCE_IN_LAB.md)

这些指令必须引用真实存在的命令和配置 schema，CI 会检查命令与版本，避免把实现细节复制进一份会漂移的大 Prompt。

## AI 允许做的事

- 解释稳定错误码和脱敏 JSON；
- 生成变更计划、检查清单和教师文案草稿；
- 在隔离环境运行自动测试和故障注入；
- 辅助生成题面及自测输入草稿，再交给本地 validator/oracle 和教师批准。

## AI 禁止做的事

- 读取、打印或上传秘密、学生源码、隐藏数据和答案；
- 直接修改 MongoDB、SQLite、Hydro 比赛、成绩或排行榜；
- 绕过 SSH 指纹、安全组、活动比赛、镜像摘要和容量门禁；
- 在未展示计划和获得批准时启动收费资源、重启 Hydro 或加载 Caddy；
- 把语言模型输出当作评测真值。
