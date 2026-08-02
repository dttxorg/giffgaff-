# Giffgaff / CTExcel Label Manager

一个轻量的 giffgaff / CTExcel 客户、托管邮箱和标签打印管理工具。项目功能包括客户管理、MoEmail / CloudMail 邮箱、订单邮件解析、JSON 导入导出和可视化标签模板。

---

## 功能总览

- **客户管理**：先按开通日期建档，邮箱可手填或生成 MoEmail，手机号和收货地址可后补
- **SIM 激活码库**：批量导入 giffgaff SIM 激活码，添加客户时可选择使用或不使用激活码，并可在库中标记不用/删除
- **人工激活资料**：分配 SIM 激活码，使用注册邮箱作为官网登录密码，并在客户详情中人工维护手机号和激活状态
- **发货资料**：在客户详情中维护收货地址、快递公司、快递单号，并独立打印快递单
- **MoEmail 集成**：客户行按钮按需生成临时邮箱，保存分享链接，拉取可用域名
- **邮箱接码**：MoEmail 客户可刷新最新邮件并自动提取 Giffgaff 6 位验证码
- **完整邮箱**：客户详情中按需读取托管邮箱的全部邮件，自动识别 Unix 秒/毫秒时间戳，同时显示北京时间与伦敦时间，并提供受控 HTML 预览、纯文本正文和复制功能
- **双产品常驻模式**：页头可在 giffgaff / CTExcel 间切换，选择保存到 SQLite；新客户自动继承当前模式，历史客户继续保留自己的产品类型
- **CTExcel 邮件建档**：同时支持 MoEmail 和 CloudMail，自动从订单/激活邮件提取并保存手机号、订单号、交易金额、推荐资料、个人中心账号和初始密码
- **CTExcel 已激活号码资料扫码页**：与 giffgaff 客户页一致，客户列表和详情页均可直接生成、打开或复制固定链接，并可快捷复制手机号、注册邮箱、个人中心账号与初始密码，同时展示 ChatGPT Plus / Pro 代充服务；存量客户缺少公开 Token 时按需补齐，后续资料更新不更换链接
- **CTExcel Windows 客户端**：先在后台新建 CTExcel 客户并取得专属邮箱，再完成注册、优惠校验与人工微信支付
- **模式隔离**：CTExcel 客户不分配 SIM 激活码、不生成英国随机身份、不显示 eSIM、支付解绑和关闭语音信箱工具
- **标签模板**：六个默认模板，可拖拽排版，支持 `50mm x 30mm` 和 `50mm x 40mm`
- **二维码打印**：模板里可放号码资料二维码、内置 12 步教程的未激活卡二维码和 Giffgaff App 下载二维码
- **导出导入**：手动导出/恢复客户、标签模板和安全设置 JSON
- **管理界面**：纯 HTML/CSS/JS，无需前端构建

---

## 架构说明

```
管理界面
    ↓
FastAPI API
    ↓
SQLite 保存客户、MoEmail 信息和系统设置
    ↓
MoEmail API 生成邮箱和分享链接
```

MoEmail 部署地址和 API Key 在管理界面的「系统设置」中保存，不依赖本地 `.env` 文件。

---

## 快速开始

### 1. 安装依赖

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 启动 Web 服务

```bash
cd backend
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

访问：

```text
http://localhost:8000
```

首次启动会自动创建数据库。旧数据库如果缺少 MoEmail 字段，或手机号字段仍是必填约束，也会在启动时自动补齐/迁移。

### 3. 设置隐藏管理入口和访问口令

如果部署到公网，配置随机隐藏入口和独立访问口令：

```bash
export ADMIN_ENTRY_PATH="$(python3 -c 'import secrets; print("/" + secrets.token_urlsafe(32))')"
export APP_PASSWORD="换成你的强口令"
uvicorn main:app --host 0.0.0.0 --port 8000
```

请把生成的 `ADMIN_ENTRY_PATH` 安全保存到密码管理器，不要提交到 GitHub、聊天记录或公开文档。部署后只能先访问：

```text
https://你的后台域名/<ADMIN_ENTRY_PATH 的随机路径>
```

入口会写入带 HMAC 签名和签发时间的 `__Host-` Cookie（`HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/`），然后跳转到登录页面。入口授权有效期为 12 小时，过期后必须重新访问秘密路径。没有入口 Cookie 时，管理页面、静态资源和普通管理 API 都只返回统一的 `404 Not found`；通过入口后仍必须输入 `APP_PASSWORD`，隐藏路径不能替代密码。密码登录 Cookie 同样使用 `__Host-` 前缀和上述安全属性。

同一客户端 IP 在 10 分钟内最多允许 5 次密码失败；继续输错会返回 `429`，正确登录后会清除该 IP 的失败记录。

由于入口 Cookie 强制使用 `Secure`，生产后台必须使用 HTTPS。`ADMIN_ENTRY_PATH` 必须是以 `/` 开头、至少 32 位的单段 URL-safe 随机路径；配置弱路径或只配置入口但不配置 `APP_PASSWORD` 时，服务会拒绝启动。本地 HTTP 开发可以同时不设置这两个变量，沿用原来的开发模式。

公开号码资料/激活教程页面 `/p/*`、Worker 回调 `/api/public/*` 不受隐藏入口影响。桌面申请只开放 `/api/ctexcel-client/*` 的建档与接码能力，并单独校验 Bearer 口令；其余管理接口都需要先通过隐藏入口与后台口令。

### 4. 配置 MoEmail

打开页面右上角「设置」，填入：

| 配置 | 说明 |
|---|---|
| MoEmail 部署地址 | 例如 `https://moemail.yourdomain.com`，不带尾部斜杠 |
| MoEmail API Key | 在 MoEmail 后台创建的 API Key |

保存后添加客户时，如果邮箱留空，系统会让 MoEmail 自动分配邮箱名，并把生成的地址写入该客户唯一的邮箱字段。客户详情里的「验证码」区域可以点击「刷新」，系统会读取该 MoEmail 收件箱最新邮件，并从 Giffgaff `Confirm it's you` 邮件中提取 6 位验证码。手填邮箱没有 MoEmail 邮箱 ID，无法自动读取邮件。

### 5. CTExcel 模式

页头的产品切换开关会把当前模式保存到系统设置，刷新页面或重新登录后仍保持上次选择。CTExcel 模式下添加客户时：

- 自动创建 MoEmail 或 CloudMail 邮箱
- 强制跳过 SIM 激活码分配
- 跳过英国姓名、地址和邮编生成
- 详情页只保留账号、CTExcel 订单、完整邮箱、发货和通用工具
- 打开详情或刷新验证码时，自动扫描订单邮件
- 手动点击“扫描订单邮件”也会把结果写回 SQLite，并同步更新首页状态

后台默认每 60 秒轮转扫描最近 14 天内、仍缺少手机号、订单号或个人中心登录资料的 CTExcel
客户邮箱，每轮最多处理 6 个。创建客户时保存的邮箱账户 ID 是订单关联键，
因此客户端支付结束后无需回写订单字段。可通过环境变量调整：

```bash
export CTEXCEL_AUTO_SYNC_INTERVAL_SECONDS=60
export CTEXCEL_AUTO_SYNC_LOOKBACK_DAYS=14
export CTEXCEL_AUTO_SYNC_BATCH_SIZE=6
# 并发桌面客户端领取待完成客户后的崩溃兜底租约，默认 45 分钟；
# 客户端正常退出或报错时会使用同一 request_key 立即释放
export CTEXCEL_CLIENT_LEASE_SECONDS=2700
```

将扫描间隔设为 `0` 可停用后台定时任务，详情页的“扫描订单邮件”仍可使用。

订单邮件解析字段包括：

```text
手机号码
订单号
交易金额
专属推荐码
专属推荐链接
个人中心账号
初始密码
```

账号和密码必须在同一封带 CTExcel 个人中心上下文的邮件中同时出现才会写入，密码中的 `*` 等字符会原样保留。客户详情支持人工编辑和快速复制；公开页固定使用 `https://www.ctexcel.com/uk/login?redirect=/personal/personalHome`，提供账号、初始密码、整段登录资料复制及直接打开个人中心按钮。

每位客户保存自己的 `product_type`，因此切换到另一模式不会改变历史客户的业务类型。CTExcel 号码资料二维码继续复用现有公开 Token 和 Worker 地址，但后端会根据客户类型渲染独立 CTExcel 扫码页；页面不包含 giffgaff 教程、eSIM、支付检查或语音信箱内容。邮件同步、支付回写或后台编辑公开字段时只提升 `public_version` 让 Worker 缓存失效，原有 `public_token` 和客户链接保持不变。

Windows 自动申请客户端位于
[`desktop-client`](desktop-client/README.md)。它通过受限的
`/api/ctexcel-client/*` 接口和现有 `APP_PASSWORD` 连接，不访问隐藏管理
入口，也不取得普通客户管理权限。客户端先创建 `product_type=ctexcel`
的客户，再使用返回的 `customer_id + email` 完成购买。支付成功页只作为
流程完成信号，订单号和手机号仍由后台从该邮箱自动提取并写入同一客户。

### 6. SIM 激活码与人工激活

在「SIM 激活码」页面批量导入激活码后，添加客户时可以选择「使用激活码」或「不使用激活码」。选择使用时系统会自动：

- 分配一个未使用 SIM 激活码
- 使用手填邮箱，或在邮箱留空时生成 MoEmail
- 使用注册邮箱作为 giffgaff 官网登录密码
- 将客户状态设为「已分配激活码」，等待人工处理

如果选择「不使用激活码」，客户会正常建档但不会关联 SIM 激活码。SIM 激活码库中可把未分配的激活码标记为「不用」，也可以删除误导入的激活码；删除已开始/已完成客户关联的激活码时，只移除码库记录并保留客户激活信息。

手机号、激活状态、验证码、支付信息邮件查询和 eSIM 信息都在客户详情中人工维护。旧数据中的「等待客户端领取」会在服务启动时迁移为「已分配激活码」。

后台客户管理也支持多域邮箱与客户重置：

```text
POST  /api/customers                            -- 新增客户，可选 email_provider_id 与 email_provider_domain
POST  /api/customers/{id}/reset                 -- 重置客户，可选还原 SIM、邮箱和激活状态
GET   /api/email-providers                      -- 返回 domain / default_domain 字段
PATCH /api/email-providers/{id}                 -- 修改 name / config / domains / default_domain
```

当 moemail provider 同时配置多个 `domains` 时，前端“添加客户”表单会显示域名选择，提交后会写入 `customers.email_provider_domain`，并透传给 provider；如果 provider 没有显式 `domains`，表单隐藏域名选择并使用其内置 `default_domain`。Cloud-Mail provider 单值绑死，每次只能配置一个域名（要换域就改 provider 的 `domain` 字段）。

支付卡解绑后，可在客户详情中人工检查 MoEmail 的 giffgaff 邮件：`your payment info has been updated` 代表支付信息更新/绑卡，`your payment info has changed` 作为取消绑定确认。

### 7. 使用标签模板

「标签模板」页面内置六个模板：

- `基础标签 50x30`
- `完整标签 50x40`
- `双码标签 50x40`
- `未激活卡教程 50x40`
- `CTExcel 号码资料 50x40`
- `快递单 50x40`

模板支持拖拽排版，变量名称使用中文，包括手机号、邮箱、开通日期、收货地址、号码资料二维码、未激活卡教程二维码和 Giffgaff 下载二维码等。未激活卡教程二维码会打开内置截图的 12 步单页教程，不再复制或跳转到第二个教程网址；号码资料二维码会自动替换为该客户的公开 Token 页面（展示已激活手机号码与初始注册邮箱），不会把演示 Token 打印出去。客户行的「快递单」按钮会固定使用快递模板，适合打印包含收货地址和快递信息的手动快递单。

---

## 数据导出 / 导入

### 导出

管理界面右上角有「导出」按钮。导出的 JSON 包含客户数据、收货地址、快递公司、快递单号、Giffgaff 下载链接、MoEmail 部署地址和标签模板。敏感邮箱凭证不会写入导出文件。

或用 API：

```bash
curl http://localhost:8000/api/export > backup.json
```

### 导入

管理界面上传 JSON 文件即可恢复。

或用 API：

```bash
curl -X POST http://localhost:8000/api/import \
  -F "file=@backup.json"
```

导入会替换现有客户数据，并恢复备份中的标签模板和安全设置。后端会先校验备份结构，并在事务中执行恢复；失败会回滚，不会先清空数据后中断。

---

## 目录结构

```
giffgaff-label-manager/
├── backend/
│   ├── main.py          # FastAPI 主程序
│   ├── database.py      # SQLite 初始化和轻量迁移
│   ├── crud.py          # 数据库操作
│   ├── moemail.py       # MoEmail API 客户端
│   ├── models.py        # Pydantic 数据模型
│   ├── requirements.txt
│   └── giffgaff.db      # 数据库（自动创建，已被 .gitignore 忽略）
├── frontend/
│   └── index.html       # 管理界面
├── desktop-client/      # CTExcel Windows 自动申请客户端
└── README.md
```

---

## 技术栈

- 后端：FastAPI + SQLite（aiosqlite）
- 邮箱：MoEmail 临时邮箱和分享链接
- 前端：纯 HTML/CSS/JS
