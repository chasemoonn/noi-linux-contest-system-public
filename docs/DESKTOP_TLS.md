# 加密桌面入口（可选配置）

更新：2026-09-09。此文描述新增的候选能力，不表示已有站点自动启用了 HTTPS/WSS。旧部署默认值不变。交付前必须在自己的域名、证书、EIP 和浏览器上完成本页验收。

2026-09-18 站点状态补充：现场站点已在明确批准的维护窗口为独立桌面域名启用可信 HTTPS/WSS，
完成实际浏览器证书/对端/WSS连接、Geany 操作、桌面 Firefox 网页递交和完整保护期自动关机。
容量与真人验收仍按 CURRENT_RELEASE.md 的最新状态记录；其他安装站点不会因此自动启用。证书有效期至
2026-12-16 19:50:55 UTC，管理员必须在到期前安排续期与实际加载校验，当前没有自动续期服务。

## 原理与范围

复用比赛服务器现有 Nginx，不增加第二套代理服务。学生浏览器通过独立桌面域名的 HTTPS/WSS 直连比赛服务器，桌面流量不经过 OJ。OJ 继续负责身份、比赛、代码与成绩；网页递交仍使用原有 HTTPS 地址。

| 通道 | TLS 模式 | 原有 HTTP 模式 |
| --- | --- | --- |
| 学生桌面 | 独立域名的 TCP 443，HTTPS/WSS | 固定 EIP 的 TCP 80，HTTP/WS |
| OJ 兼容桌面代理 | 关闭，不作为明文回退通道 | 保留原有兼容入口 |
| 比赛机 HTTP 就绪检查 | 仅绑定 `127.0.0.1:80` | 使用原有监听地址 |
| 学生公网安全组规则 | 比赛生命周期内仅开放配置的 TCP 443 | 原有 TCP 80 |
| 静态运维规则 | OJ 唯一 `/32` 的精确 TCP 22、80、443 | OJ 唯一 `/32` 的精确 TCP 22、80 |

443 管理规则供 OJ 在放行学生前验证页面与 WebSocket。80 管理规则暂时保留以兼容既有运维流程，但 TLS 模式的 HTTP 服务仅监听回环地址。不要配置公开的 80 入站规则。

Nginx 到同一比赛机上的隔离 Docker 网桥仍使用 HTTP；桌面内 Firefox 到本机递交代理的第一跳也是私有 HTTP。递交代理到公网 OJ 使用经过证书验证的 HTTPS。因此这里的承诺是**公网桌面加密直连**，不是所有内部进程之间都启用 TLS。

## 启用前必须由管理员确认

1. 当前没有活动比赛、未完成收卷或待核对的递交。保留已知可用版本和原配置；不要在学生操作中切换网关。
2. 分配专用桌面域名。其 DNS 必须只解析到当前比赛机的固定 IPv4 EIP，不能残留其他 A/AAAA 地址。不要改动 OJ、管理后台或网页递交域名的现有解析。仅有动态公网 IP 的部署不适用本模式。
3. 在比赛机安装浏览器信任的完整证书链及对应私钥。证书必须包含专用桌面域名，私钥仅管理员可读、不能挂载进学生容器。证书不能通过聊天、公开仓库或测试报告传递。不要用自签名证书要求学生忽略浏览器警告。
4. 用运维账号在目标专属安全组一次性添加 OJ `/32` 的精确 TCP 443 管理规则；保留 22/80 管理规则，核对不存在其他或重复入站规则。编排服务不会替管理员建立这些规则，也不会删除不属于它的规则。
5. 将桌面域名加入编排服务与 Hydro 插件两侧的精确 HTTPS 通知白名单，保留原网页递交域名。按现有部署流程应用配置，不要重装或改动无关 OJ 功能。

已有比赛机还需将本版本的 `deploy/noi-wait-nginx-bind-addresses` 安装到原 `/usr/local/sbin/noi-wait-nginx-bind-addresses`。新版识别 `0.0.0.0` 为通配监听，避免冷启动错误等待一个不可能出现的网卡地址；对明确的 Docker 网桥 IP 仍等待就绪。只更新这一文件，不必为此重建网络或重装整台主机。

## 配置示例

以下是需合并的字段，不是可以覆盖整份配置的文件。示例域名和路径必须替换为已核实的值：

```yaml
cloud:
  aliyun:
    desktop_access:
      enabled: true
      port: 443
      # 保留已核实的 security_group_id、source_cidr 和 management_source_cidrs
contest_server:
  gateway_listen: 80
  gateway_bind_address: "0.0.0.0"
  gateway_scheme: "https"
  gateway_public_base_url: "https://desktop.example.com"
  gateway_tls:
    enabled: true
    certificate: "/etc/noi-tls/fullchain.pem"
    certificate_key: "/etc/noi-tls/privkey.pem"
hydro:
  notify_allowed_https_hosts:
    - "exam.example.com"
    - "desktop.example.com"
```

目前限受管 Aliyun `desktop_access`，且只支持公网 443。`frontend_proxy` 仍保留原 Caddy 管理配置，用于确保旧桌面代理被关闭，不承载 TLS 模式的桌面流量。无需新购服务器或新增商业功能。

## 自动检查与错误处理

- 配置加载：检查开关类型、域名、端口、证书路径和通知白名单的组合。仅设置 `gateway_scheme: https` 不足以启用原生 TLS。
- 开机之前：DNS 与固定 EIP 不符时直接终止备赛，不启动比赛主机。
- 创建座位之前：检查证书/私钥可读，使用系统信任库验证证书链和域名；有效期必须至少覆盖当前已知比赛结束后 30 分钟，且不少于一小时。使用 `openssl verify` 判断失败，不依赖旧版 `x509 -checkhost` 的退出状态。
- 发布学生入口之前：`nginx -t` 校验配置与证书私钥配对；OJ 通过受信任 TLS/SNI 实际请求座位页面，要求 HTTP 200，再请求 WebSocket 升级，要求 101 和正确握手响应。只检测 443 打开或页面能显示，不算通过。
- 截止：沿用同一比赛时间、冻结及收卷机制；移除整个比赛网关配置同时覆盖 HTTPS 与本机 HTTP 监听。

失败时不会静默退回 HTTP。管理员先处理明确的 DNS、证书或入站规则问题，再重新执行准备，不要跳过校验。证书安装、续期和 Nginx 重新加载仍由管理员负责；系统尚未提供自动签发或续期。延时或跨证书更新的长场次须重新确认实际加载的证书有效期，不要仅核对磁盘上的新文件。

## 上线验收（仍需实机完成）

在隔离的单人测试比赛中确认：私信里的入口是正确的 HTTPS 专用域名；浏览器没有证书警告；noVNC 真正建立 WSS 且能操作；断开重连后键盘正常；Geany 编译运行、桌面 Firefox 网页递交和 OJ 回执一致；网页来源不被目录覆盖；截止后入口和写代码权限关闭，已有代码仍能送评及核对。

确认 TLS 模式没有旧 OJ 桌面回退链接。随后才开展真实人数的容量测试和合作教练独立办赛。回环 TLS 测试、模拟云 API 和自动代码回归不能替代这些证据。

容量测试使用独立的 `direct_https` 档位，冻结实际比赛机 IPv4、座位 URL 和 WSS 路径，接收器显式指定
`--transport-profile direct_https`。旧的 `direct_http` 与 `compat_https` 记录不能充当原生 TLS 容量结果。
配置方法和记录边界见 [15+2 容量验收](../deploy/V1_CAPACITY_REHEARSAL.md)。

如需回退，先结束并核实测试场次及待处理队列，再恢复旧版本/配置，同时由管理员处理仅为 TLS 添加的精确 443 管理规则，使安全组与旧的 22/80 合同一致。不要只改 `gateway_scheme`、删证书、删除整个安全组或覆盖学生文件。HTTP 回退是管理员明确选择的临时模式，不是商业交付已达标。

参考实现依据：[Nginx HTTPS 配置](https://nginx.org/en/docs/http/configuring_https_servers.html)、[Python HTTPSConnection](https://docs.python.org/3/library/http.client.html)、[OpenSSL 3.0 x509 实现](https://github.com/openssl/openssl/blob/openssl-3.0.16/apps/x509.c)。
