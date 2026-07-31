# public-card Worker

Cloudflare Worker：把扫码公开页 (`/p/{token}`) 部署到边缘，30 天版本化缓存。

## 部署方式

### 方式 A：GitHub 自动部署（推荐）

1. 把这个目录 (`worker-src/`) 推到 GitHub（它已在 `dttxorg/giffgaff-label-manager` 仓库里）
2. Cloudflare 控制台 → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
3. 选择 `dttxorg/giffgaff-label-manager` 仓库
4. 配置：
   - **Project name**: `public-card`（可改）
   - **Production branch**: `main`
   - **Build command**: 留空
   - **Build output directory**: 留空
   - **Root directory** (advanced): `worker-src`
5. 点 **Save and Deploy**
6. 进 **Settings** → **Variables and Secrets** → 确认或新增：
   - `API_BASE` = `https://gg.6667766.xyz`
   - 必须配置在 Production Worker 运行时的 **Variables and Secrets**，不是 GitHub 构建变量
   - Worker 会自动清理一个或多个尾部斜杠，但建议仍按上面的无尾斜杠格式填写
7. 重新触发部署（Deployments → Retry）

以后改代码 push 到 main，Cloudflare 自动部署。

### 方式 B：手动 wrangler CLI

```bash
cd worker-src
npm install
# 编辑 wrangler.toml 把 API_BASE 改成你的 admin URL
wrangler deploy
```

### 绑自定义域名（两种方式都要做）

1. Cloudflare 控制台 → Workers & Pages → `public-card` → **Settings** → **Triggers** → **Custom Domains** → **Add Custom Domain**
2. 输入子域，例如 `card.example.com` → **Add Custom Domain**
3. DNS 自动配好

### 回到 admin 填域名

你的 admin → **Worker 部署** 标签页 → 顶部「Worker 域名」输入框 → 填 `https://card.example.com` → 保存

之后客户扫码会走 `card.example.com`。版本信息在边缘缓存 60 秒，窗口内的页面
缓存命中不会逐次回源；源站短暂故障时，已缓存页面可使用 24 小时内的旧版本
元数据继续提供服务。明确的版本 `404` 仍立即撤销旧 Token。

## 验证

F12 → Network → 扫码访问 `/p/{token}`：

- 正常响应应为 `200`
- 应包含 `X-Public-Card-Worker: 7`
- 第一次访问应为 `X-Cache: MISS`，再次访问应为 `X-Cache: HIT`
- `X-Version-Cache` 会显示 `FRESH`、`REFRESHED` 或故障回退时的 `STALE`
- 如果看到 `X-Worker-Error: api-base`，检查 Production 运行时的 `API_BASE`
- 如果看到 `X-Origin-Stage` / `X-Origin-Status`，按版本接口或页面接口的源站状态排查

本地运行 Worker 专项测试：

```bash
cd worker-src
npm test
```

## 回滚

Cloudflare → Workers & Pages → `public-card` → **Deployments** → 选旧版本 → **Rollback to this deploy**。
