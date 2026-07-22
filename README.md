# UPI Link Extractor

从 `any-auto-register` 中拆出的单用途 UPI 提链项目。它接收本人或已获授权账号的 ChatGPT Access Token，调用原项目的纯 HTTP UPI 核心，输出 Stripe UPI 支付长链和二维码。

## 功能

- Access Token 或含 `accessToken` 字段的 JSON 输入
- 自动从 JWT/JSON 解析账号邮箱，也允许手工填写
- 后台任务、状态轮询、日志和取消
- Stripe UPI 支付链接和二维码展示
- 可选登录代理、India 代理池和 Approve 参数
- Web 页面与命令行两种入口
- Token 不写数据库、不进入任务响应、不保存到浏览器存储

## 安全边界

- 仅用于你本人或明确授权的账号。
- 当前版本不接收或交换 Session Cookie；请提供 Access Token。
- 默认仅监听 `127.0.0.1`。不要直接暴露到公网，因为接口接收高敏感凭证。
- 运行依赖 ChatGPT/Stripe 的非公开页面接口，第三方改版后可能失效。
- 代理不是必需项；请只使用你有权使用的代理服务。

## 安装与启动

建议使用 Python 3.11～3.13：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 15336
```

Windows 也可以在依赖安装完成后双击 `start.bat`。

浏览器打开：<http://127.0.0.1:15336>

运行时文件位于：

- `runtime/qr/`：生成的二维码
- `runtime/cache/stripe_bundles/`：自动获取的 Stripe JS 缓存

## Docker

使用预构建的 GHCR 镜像：

```powershell
docker run -d --name upi-link-extractor --restart unless-stopped `
  -p 127.0.0.1:15336:15336 `
  -v "upi-link-runtime:/app/runtime" `
  ghcr.io/xiaoxin-zk/upi-link-extractor:latest
```

或者使用 Compose：

```powershell
docker compose up -d
```

本地构建：

```powershell
docker build -t upi-link-extractor:local .
docker run -d --name upi-link-extractor -p 127.0.0.1:15336:15336 upi-link-extractor:local
```

容器默认以非 root 用户运行，并提供 `/api/health` 健康检查。部署示例使用 Docker 命名卷，避免宿主机 bind mount 的 UID/GID 权限差异。公网部署前必须增加 HTTPS 和访问认证。

## 命令行

推荐通过环境变量或凭证文件提供 Token，避免写入命令历史：

```powershell
$env:CHATGPT_ACCESS_TOKEN="你的 Access Token"
python -m upi_link.cli --email "account@example.com"
```

或者：

```powershell
python -m upi_link.cli --credential-file "credential.json" --proxy-file "proxies.txt"
```

## API

- `POST /api/jobs`：创建提链任务
- `GET /api/jobs`：列出本次服务进程中的任务
- `GET /api/jobs/{id}`：查询状态
- `POST /api/jobs/{id}/cancel`：取消任务
- `GET /api/jobs/{id}/qr`：读取二维码

任务信息仅保存在内存中。服务重启后任务列表会清空，已生成的二维码文件仍保留。

## 测试

测试不会请求 ChatGPT 或 Stripe：

```powershell
python -m pytest
```

## 许可证

本项目为 AGPL-3.0 派生项目。核心来源及声明见 `NOTICE`，完整许可证见 `LICENSE`。
