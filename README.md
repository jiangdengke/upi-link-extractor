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
- 管理员密码登录和 CDK 兑换码管理
- 管理员统一配置代理池、登录代理、重试、并发和代理步骤
- CDK 次数、有效期、停用、成功扣次与失败释放
- “仅提链”和“提链 + Foarge 支付”两种 CDK
- 支付型任务自动等待上游、提交长链、同步支付进度并按需刷新过期长链
- 浏览器任务隔离，用户之间不会看到对方的任务、日志和支付链接
- 批量 API 一次最多提交 10 个 Access Token / Session JSON

## 安全边界

- 仅用于你本人或明确授权的账号。
- 当前版本不接收或交换 Session Cookie；请提供 Access Token。
- 默认仅监听 `127.0.0.1`。不要直接暴露到公网，因为接口接收高敏感凭证。
- 运行依赖 ChatGPT/Stripe 的非公开页面接口，第三方改版后可能失效。
- 代理不是必需项；请只使用你有权使用的代理服务。
- Access Token 不会持久化。使用支付型 CDK 时，服务会按 Foarge Publisher API
  的要求把 Access Token 与 UPI 长链发送至 `https://foarge.com/api/publisher/v1`
  用于支付和 Plus 验单。

## 安装与启动

建议使用 Python 3.11～3.13：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 15336
```

Windows 也可以在依赖安装完成后双击 `start.bat`。

浏览器打开：<http://127.0.0.1:15336>

管理员页面：<http://127.0.0.1:15336/admin>

代理池和提链运行参数只在管理端配置。用户页面和公开任务 API 无法读取或覆盖代理凭证。

### Foarge 支付型 CDK

管理员可在管理页面的“Foarge 支付配置”中保存 `PBK-...` CDK，并检查剩余次数。
随后生成 CDK 时选择“提链 + Foarge 支付”。Foarge PBK 只保存在 SQLite 数据卷中，
管理 API 仅返回脱敏状态，用户 API 不返回该密钥。

支付任务遵循 Foarge 官方时序：先创建并等待 `awaiting_checkout`，再生成 UPI
长链并立即提交。用户页面持续显示上游支付状态；上游要求刷新时会自动重新提链，
不会重复扣本地 CDK 或 Foarge PBK。首次长链生成成功即消耗一次本地 CDK；提链前
失败或取消则释放本地次数。

运行时文件位于：

- `runtime/qr/`：生成的二维码
- `runtime/cache/stripe_bundles/`：自动获取的 Stripe JS 缓存

## Docker

首次部署先创建 `.env`：

```bash
cp .env.example .env
nano .env
```

其中 `UPI_ADMIN_PASSWORD` 填写你自己的强密码；`UPI_SESSION_SECRET` 可以通过
`openssl rand -hex 32` 生成。两项为空时 Compose 会拒绝启动。

如果通过 HTTPS 域名访问，把 `.env` 中的 `UPI_COOKIE_SECURE` 改为 `1`。
如果只允许服务器本机或反向代理访问，把 `UPI_BIND_HOST` 改为 `127.0.0.1`。

使用预构建的 GHCR 镜像：

```powershell
docker run -d --name upi-link-extractor --restart unless-stopped `
  -p 0.0.0.0:15336:15336 `
  -v "upi-link-runtime:/app/runtime" `
  -e UPI_ADMIN_PASSWORD="你的强管理员密码" `
  -e UPI_SESSION_SECRET="独立随机密钥" `
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
- `POST /api/jobs/batch`：批量创建任务，最多 10 项
- `GET /api/jobs`：列出本次服务进程中的任务
- `GET /api/jobs/{id}`：查询状态
- `POST /api/jobs/{id}/cancel`：取消任务
- `GET /api/jobs/{id}/qr`：读取二维码
- `POST /api/cdk/verify`：检查 CDK 次数和有效期
- `POST /api/admin/login`：管理员登录
- `POST /api/admin/cdks`：生成 CDK
- `GET /api/admin/cdks`：列出 CDK
- `GET /api/admin/foarge`：读取脱敏的 Foarge 配置状态
- `PUT /api/admin/foarge`：保存或清除 Foarge PBK
- `POST /api/admin/foarge/check`：检查 Foarge PBK 状态

批量任务请求示例。建议把内容保存为仅当前用户可读的 JSON 文件，避免 Token 进入 Shell 历史：

```json
{
  "cdk": "UPI-XXXX-XXXX-XXXX",
  "items": [
    {
      "credential": "ACCESS_TOKEN_1",
      "email": "account1@example.com"
    },
    {
      "credential": "{\"accessToken\":\"ACCESS_TOKEN_2\",\"user\":{\"email\":\"account2@example.com\"}}"
    }
  ],
  "authorized": true
}
```

提交：

```bash
chmod 600 batch-request.json
curl -X POST http://127.0.0.1:15336/api/jobs/batch \
  -H 'Content-Type: application/json' \
  -c upi-cookie.txt -b upi-cookie.txt \
  --data-binary @batch-request.json
```

批量 API 使用浏览器会话 Cookie 隔离任务。后续查询 `/api/jobs` 时需要继续携带同一个 Cookie 文件。
批量任务和单任务都会自动使用管理员端保存的全局代理配置。

任务信息仅保存在内存中。服务重启后任务列表会清空，已生成的二维码文件仍保留。

## 测试

测试不会请求 ChatGPT、Stripe 或 Foarge：

```powershell
python -m pytest
```

## 许可证

本项目为 AGPL-3.0 派生项目。核心来源及声明见 `NOTICE`，完整许可证见 `LICENSE`。
