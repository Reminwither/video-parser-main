# 视频解析工作台 · 线上部署记录

最近核验：2026-10-01。

## 公网入口

- 主站：https://110.40.138.167
- 健康检查：https://110.40.138.167/health
- 服务器：腾讯云轻量应用服务器 lhins-8one04is，公网 IP 110.40.138.167，2 核 2GB、50GB 磁盘。
- 当前还没有域名。Caddy 使用 Let's Encrypt 短期 IP 证书；Certbot 定时检查续期并在成功续期后重载 Caddy。

## 当前部署

- GitHub master 推送触发 .github/workflows/deploy.yml；服务器代码位于 /opt/video-parser，镜像在本机构建。
- 主站与后台发布分别读取服务器仓库的提交基线，兼容时只传缺失的 Git 历史；首次部署、基线缺失、不同历史或同版本重建均回退全量。传输前和导入前验证 bundle，SSH 保留固定主机指纹并设置连接超时。
- Caddy 对外监听 80/443，80 保留证书签发挑战并将其余请求重定向到 HTTPS。主站容器只绑定 127.0.0.1:7860。
- 管理后台容器只绑定 127.0.0.1:7861，由 Caddy 在 https://110.40.138.167/admin/ 提供 HTTPS 入口。使用现有管理员账号登录。
- 腾讯云防火墙已删除旧的 TCP 7860 公网放行规则，保留 80/443 与 SSH。原有的 18888 规则未改动；当前没有进程监听该端口，其用途还需确认。
- 账号、下载、缓存、日志、静态视频和图片均挂载持久化目录。2026-09-28 公网 `/register` 已确认开放且有注册表单；是否开放由 `ALLOW_REGISTER` 控制。登录会话 Cookie 设置 Secure。
- 认证数据库由 video-parser-auth-backup.timer 每天在线备份到 /var/backups/video-parser，保留 14 份；同时上传至上海地域 COS，上传时启用 AES256 服务端加密，再下载核对 SHA256 与 SQLite 完整性。2026-10-01 03:35 最近一次备份、异地上传和恢复检查成功；本次只读核验未重新查询 COS 的访问权限或生命周期保留期。

## 验证与维护

- GitHub 主站与后台部署工作流已成功完成。公网 /health 和首页返回 200，未登录账户页跳转登录页，未登录业务 API 返回 401。
- 2026-10-01 正式证书已自动续期，有效期至 2026-10-08 02:22:19（北京时间）。video-parser-certbot-renew.timer 每 12 小时运行检查，最近一次服务于 2026-10-01 11:20:57 成功退出；renewal 配置调用 /usr/local/bin/video-parser-cert-deploy，复制证书后校验并重载 Caddy。站外探针验证 CA 信任链与 IP 匹配，剩余有效期不超过 24 小时时报告失败；2026-10-01 公网 TLS 握手与提前到期探针通过。
- 当日发现普通 reload 因配置文本未改变而跳过新证书加载，公网仍提供旧证书。已修正续期 hook 为 `caddy reload --force`，并核对本机实际提供的证书与安装文件指纹一致；公网经 CA/IP 验证确认新证书已生效。旧 hook 备份位于 `/usr/local/bin/video-parser-cert-deploy.pre-force-20261001`。
- 服务器 .env 权限为 600，备份目录仅 root 可读。不要把 .env、数据库备份或 API 密钥提交到仓库。
- 服务器本地仍保留 /opt/Dockerfile.prod 作为国内镜像源构建配置。

历史上曾通过 http://110.40.138.167:7860 直连；该入口已停用。域名购入后，可将 Caddy 与 DOMAIN 改为域名，并切换到常规自动 HTTPS 证书。

主站部署前会运行 `npm ci --ignore-scripts`、`npm run test:auth`、安装 `ops/validation-requirements.txt` 后执行 `python3 -m unittest test_quota_display test_auth_payload test_report_metrics test_private_files`，并检查 Python 语法。后台部署前也执行报告统计检查和语法检查。认证弹窗的 DOM 行为检查使用 Node.js 24.16.0 和 jsdom；它不测布局，不发生产请求，也不处理用户凭据。报告统计检查使用隔离内存数据库与实际 ASGI 中间件和后台路由，覆盖旧表升级、导出去重、中断传输、反馈更新和账号删除。

后台部署通过 `ops/deploy-admin-container.sh` 先构建候选镜像，再重建容器；检查本机数据库健康、登录入口、匿名看板重定向及公网 HTTPS。启动或验证失败时恢复部署前的确切镜像 ID；构建失败不停止现有容器。首次部署没有可恢复镜像时会明确报错。4 项 Bash 故障演练使用模拟 Docker/HTTP，覆盖成功、构建失败、启动/健康/访问边界失败和恢复失败，并加入后台发布门槛。

2026-10-01 代码 `dc81588` 的主站发布 `36878365247` 与后台发布 `36878365282` 已通过；公网用三个临时账号完成 33 项报告下载/反馈/后台指标/账号删除检查后清理数据。本轮的 46 项不同自动检查与真实 HTTP 验收均通过；手机视觉及软键盘体验仍未验证，本轮未重新调用 AI/ASR。

下一阶段新增 `processing_tasks` / `task_resource_usage`，迁移只创建表与索引，兼容旧版本。全站与任务用量在同一事务中记录，当前处理线程通过 ContextVar 隔离任务；任务完成始终恢复上下文并释放处理槽。用户处理中删除账号时不再写入其任务明细，已消耗资源保留在全站聚合。发布前新增 `test_task_usage` 和 `test_processing_attribution`；后台统计可在主站尚未升级时显示等待状态。
