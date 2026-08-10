# 第三方组件与来源说明

本项目编排和集成了多个独立组件。完整离线交付包必须同时保存对应版本、来源、许可证和哈希；本文件不是第三方许可证正文的替代品。

| 组件 | 用途 | 来源与注意事项 |
| --- | --- | --- |
| Hydro | OJ、比赛、评测记录和站内通知 | <https://github.com/hydro-dev/Hydro>；保留 Hydro 来源署名并遵守其许可证与附加条款 |
| NOI Linux 2.0 | 模拟赛桌面基础环境 | <https://www.noi.cn/>；离线镜像 manifest 记录 ISO SHA256 和构建来源 |
| Ubuntu | 基础操作系统与软件包 | <https://ubuntu.com/> |
| GNOME | 官方桌面环境 | <https://www.gnome.org/> |
| TigerVNC | VNC 服务端 | <https://tigervnc.org/> |
| noVNC | 浏览器远程桌面客户端 | <https://novnc.com/> |
| Docker / containerd | 学生容器隔离与镜像分发 | <https://www.docker.com/> |
| Caddy | Hydro 和考试控制面反向代理 | <https://caddyserver.com/> |
| nginx | 比赛机座位路由与 WebSocket 代理 | <https://nginx.org/> |
| FastAPI / Uvicorn | 编排器 Web 服务 | <https://fastapi.tiangolo.com/> |

发布前应从实际锁定的依赖、桌面镜像和离线包生成 SBOM，并把未列出的传递依赖补入 Release 资产。
