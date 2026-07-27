# UPI / Kakao Link Extractor

接收 ChatGPT Access Token，通过纯 HTTP 流程输出 Stripe UPI 支付长链与二维码，或韩国 Kakao Pay / Nicepay 跳转链接。

## 功能

- Access Token 或含 `accessToken` 字段的 JSON 输入
- 自动从 JWT/JSON 解析账号邮箱，也允许手工填写
- 后台任务、状态轮询、日志和取消
- Stripe UPI 支付链接和二维码展示
- 韩国 Kakao Pay / Nicepay 跳转链接提取
- 可选登录代理、India 代理池和 Approve 参数
- 独立的韩国 Kakao sticky 代理 Seed 池，不会覆盖 UPI 代理
- Web 页面与命令行两种入口
- Token 不写数据库、不进入任务响应、不保存到浏览器存储
- 管理员密码登录和 CDK 兑换码管理
- 管理员统一配置代理池、登录代理、重试、并发和代理步骤
- CDK 次数、有效期、停用、成功扣次与失败释放
- “仅提链”和“提链 + Foarge 支付”两种 CDK
- 支付型任务自动等待上游、提交长链、同步支付进度并按需刷新过期长链
- Foarge 一次性 PBK 码池，支持批量添加、原子领取和已用状态追踪
- 浏览器任务隔离，用户之间不会看到对方的任务、日志和支付链接
- 批量 API 一次最多提交 10 个 Access Token / Session JSON

## 安全边界

- 仅用于你本人或明确授权的账号。
- 当前版本不接收或交换 Session Cookie；请提供 Access Token。
- 默认仅监听 `127.0.0.1`。不要直接暴露到公网，因为接口接收高敏感凭证。
- 运行依赖 ChatGPT/Stripe 的非公开页面接口，第三方改版后可能失效。
- UPI 可按部署环境直连；Kakao 模式需要在管理端配置韩国代理 Seed。
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

UPI 与 Kakao 代理池及提链运行参数只在管理端配置。用户页面和公开任务 API 无法读取或覆盖代理凭证。

### 韩国 Kakao 提链

在管理页“韩国 Kakao 代理 Seed”中每行保存一个 sticky 代理。带
`country`/`region` 选择器的 Seed 会自动派生
`KR checkout → VN promotion → KR Stripe/Kakao` 链路。含 `{SID}` 的模板会让
checkout/provider 共用一个韩国 sticky session，并为 VN promotion 物化独立 session，
避免代理把跨地区请求锁在首个出口；普通韩国代理会在三个阶段复用原地址。任务执行前
会验证各阶段出口国家。

用户页选择“韩国 Kakao Pay”后提交 Access Token。成功结果是 Nicepay/Kakao 的
HTTPS 跳转链接，不生成 UPI 二维码；该模式不使用 Foarge 支付型 CDK。

### Foarge 支付型 CDK

管理员可在管理页面的“Foarge 支付配置”中每行添加一个 `PBK-...` CDK，并批量
检查状态。随后生成 CDK 时选择“提链 + Foarge 支付”。Foarge PBK 及其
`可用 / 占用中 / 已用` 状态只保存在 SQLite 数据卷中，管理 API 仅返回脱敏状态，
用户 API 不返回该密钥。`0.3.0` 保存的单个 PBK 会在升级时自动迁移到码池。

每个支付任务原子领取一个可用 PBK，并在任务进行期间标记为“占用中”。只有 Foarge
任务状态同步为 `completed` 后才永久标记“已用”；任务失败、取消或过期后释放回
“可用”，可以再次领取。无效或已耗尽的 PBK 标记为已用。网络中断等结果未知的任务
继续保持占用，管理员点击“检查上游”后会按上游终态自动对账，避免误用或浪费。

支付任务遵循 Foarge 官方时序：先创建并等待 `awaiting_checkout`，再生成 UPI
长链并立即提交。用户页面持续显示上游支付状态；上游要求刷新时会自动重新提链，
不会重复扣本地 CDK。首次长链生成成功即消耗一次本地 CDK；提链前
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
宿主机 Nginx 反代到 Docker 映射端口时，再把 `UPI_FORWARDED_ALLOW_IPS` 改为 `*`，
使 Uvicorn 接受 Docker 网关转发的协议头，并生成正确的 HTTPS 二维码 URL。

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

- `POST /api/jobs`：创建提链任务，`link_type` 可为 `upi` 或 `kakao`
- `POST /api/jobs/batch`：批量创建任务，最多 10 项，支持同样的 `link_type`
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

### 对接 Turb GPT Free Register

本项目提供了与 `turb-gpt-free-register/core/extract_link_service.py` 当前协议兼容的接口：

- `GET /api/cdk?code=...`：查询 CDK 状态
- `POST /api/extract`：按 `{token, link_type, cdk}` 创建任务
- `GET /api/jobs/{id}/events?cdk=...`：通过 SSE 返回日志和提链结果

先启动本服务并在管理页面生成“仅提链”CDK，然后在注册项目的 `.env` 中配置：

```dotenv
EXTRACT_LINK_API_BASE=http://127.0.0.1:15336
EXTRACT_LINK_CDK=UPI-XXXX-XXXX-XXXX
EXTRACT_LINK_TYPE=upi
```

注册项目现有的单账号、批量提链、状态保存和 WebUI 展示逻辑可以保持不变。兼容接口
接受 `upi` 和 `kakao` 类型；任务通过 CDK 隔离，UPI 结果中的二维码使用仅限对应任务的签名 URL，
不会把 CDK 放入图片地址。部署时应设置稳定的 `UPI_SESSION_SECRET`，确保服务重启后
已签发的二维码 URL 仍可验证。`EXTRACT_LINK_API_BASE` 所使用的地址也需要能从 WebUI
所在浏览器访问，否则页面只能保存支付长链，不能直接加载二维码图片。

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
