# 缓存官方 rootfs 一次性认证

`attest-cached-noi-rootfs-once.sh` 只用于已人工核对的历史缓存镜像：

```text
sha256:fed2063bb95263b9241368420215a4acc538e0f0253b3f4b51bdc4e1769c7631
```

它只给镜像配置补入官方 ISO 摘要标签
`org.noi.iso.sha256=c8824240736352e5e4aaf3f6532b40961f75fa9f23d670bb78881355a49d5878`，
不重打包、挂载或修改任何 rootfs 层。命令没有接入常规构建、部署或自动回退路径；其他镜像 ID
一律拒绝，不能用它绕过 ISO 校验。

## 显式执行

确认主机没有并发镜像构建或发布任务后执行：

```bash
sudo bash deploy/attest-cached-noi-rootfs-once.sh
```

也可以显式指定目标标签和一个尚不存在的回滚标签：

```bash
sudo bash deploy/attest-cached-noi-rootfs-once.sh \
  noi-linux-official-rootfs:2.0 \
  noi-linux-official-rootfs:rollback-pre-attest-20260808
```

只有以下安全门全部通过后才会切换正式标签：

1. 正式标签精确指向唯一允许的旧镜像 ID；
2. 回滚标签和临时候选标签都不存在，且回滚标签成功指向旧 ID；
3. Docker 导出配置的 SHA256 与旧镜像 ID 一致，归档只有一个 manifest，层引用完整；
4. 新旧 `RootFS.Layers` 的 Docker JSON 输出逐字相同；
5. ISO 标签精确匹配；
6. 在无网络、只读、丢弃 capabilities 的容器中核对 Ubuntu 20.04、GCC/G++ 9.3.0、FPC 3.0.4、GDB、DDD、Code::Blocks、Lazarus、Geany、IBus、智能拼音和 Arbiter-local；
7. 提升前再次确认正式标签和回滚标签仍指向旧 ID。

正式标签通过单次 `docker image tag` 切换。切换后验证失败时，退出陷阱会立即恢复旧 ID。
成功后仍保留回滚标签并输出其精确名称。成功后的正式标签不再是唯一允许的旧 ID，因而脚本不能重复执行。

## 人工回滚

使用脚本成功时输出的回滚标签：

```bash
sudo docker image tag \
  noi-linux-official-rootfs:rollback-pre-attest-YYYYmmddTHHMMSSZ \
  noi-linux-official-rootfs:2.0
```

随后确认正式标签的 ID 为
`sha256:fed2063bb95263b9241368420215a4acc538e0f0253b3f4b51bdc4e1769c7631`。
