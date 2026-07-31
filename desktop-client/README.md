# CTExcel 自动申请客户端（Windows）

该客户端只负责 CTExcel 新客户的申请购买流程：

1. 使用独立的 CTExcel 限权 API 和 `APP_PASSWORD` 建立连接。
2. 在客户管理列表中新建一个 `ctexcel` 客户。
3. 客户管理系统通过 MoEmail / CloudMail 自动生成专属邮箱，并返回
   `customer_id + email`。
4. 客户端按所选路线完成 CTExcel 注册、邮箱验证码、地址和微信支付。
5. 支付成功后客户端流程结束。
6. 客户管理系统后台继续扫描该专属邮箱，把订单确认状态和邮件中可用的
   补充资料写入同一客户记录。

客户端不访问隐藏管理入口、不使用旧的 Agent Token。支付二维码生成后先回写
订单号和付款金额；支付成功页出现后立即结束本单。客户端不读取手机号，也不把
手机号作为付款或连续申请的完成条件。订单确认状态和其他补充资料继续通过
**同一个专属邮箱**自动关联。

客户管理请求遇到 Cloudflare 408/5xx、连接中断或不完整响应时，会先快速退避，
随后每 30 秒继续重试，最长覆盖约 5 分钟；点击“停止”可中断等待。建档请求
始终携带“优先复用待完成客户”，即使服务器已经创建客户但 Cloudflare 丢失了
响应，下一次重试也会复用同一记录，不会因临时回源故障额外建立客户。

## 两条申请路线

### 预存 £1 领卡

- 入口：`https://www.ctexcel.com/freecard/home`
- 选择“还没选好套餐，先预存£1领卡”
- 实体 SIM、免费随机号码
- 推荐人号码：`447942946765`
- 付款金额强制校验：`£1.00`

### 50GB 套餐（保留）

- 50GB，£11.9/30天
- 实体 SIM、免费随机号码、1个月、1张
- 自动续订关闭
- 推荐码：`NTKWJX`
- 优惠码：`DEAL50OFF`
- 优惠后价格强制校验：`£5.95`

### 共同步骤

- 寄送国家：中国
- 地址：使用 CTExcel「智能填写」
- 支付方式：微信
- 人工扫码支付
- 每次关键点击前后等待网站 `Loading` 遮罩完全消失并稳定
- 浏览器操作最低间隔 800ms，加载遮罩最长等待 90 秒

姓名、联系电话和中国收货地址在客户端设置里配置一次。客户邮箱始终由
客户管理系统新建客户时生成。

### 浏览器兼容模式

每一单使用独立的临时 Chrome / Edge 配置目录，避免上一单的 Cookie、
sessionStorage 和活动订单状态污染下一单。客户端会过滤浏览器的
`--enable-automation` 启动参数，并在页面加载前移除明显的
`navigator.webdriver` 标记；关闭当前单后自动清理临时配置。

客户端只以已保存的订单号、付款金额和支付成功页 URL 判断本单完成。它不会
读取成功页正文、恢复隐藏订单参数、拦截订单接口或等待号码分配；因此官网成功页
持续显示 Loading，也不会卡住本单或连续申请。HTTP 错误、请求失败和页面脚本
错误仍会写入诊断目录中的 `network.txt`。

每次开始新申请前，服务器还会扫描无手机号客户的专属邮箱。发现主题为
`【CTExcel】您的订单已确认！` 的邮件后，会把该客户永久标记为注册成功；
即使邮件尚未提供订单号和手机号，客户端也会跳过该邮箱并创建下一位客户，
避免把已经成功领取的账号再次提交到官网。

## 连续申请

勾选“连续申请”并填写目标数量（1–1000）后，客户端按顺序逐单执行：

1. 复用未生成订单的中断客户，或新建 CTExcel 客户和专属邮箱。
2. 自动填写并停在微信二维码支付页。
3. 等待人工扫码付款。
4. 检测到“订购成功/支付成功”页面后，记录本单完成并进入下一单。
5. 达到目标数量后结束本轮。

历史版本已写入订单号和付款金额、但尚未补全手机号的客户不会阻塞下一单。
流程中断时会保留已完成数量；点击“重试第 N 单并继续”后从该单恢复，不会
重复已经完成的数量。

## 开发运行

```powershell
cd desktop-client
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

客户端默认使用 Windows 自带的 Microsoft Edge（Playwright `msedge`
channel），无需额外下载浏览器。

## Windows 打包

```powershell
cd desktop-client
.\build_windows.ps1
```

输出：

```text
desktop-client\dist\CTExcelApplyClient\CTExcelApplyClient.exe
```

GitHub Actions 的 `windows-client.yml` 也会生成同名构建产物。

## 浏览器代理

客户端提供三种模式：

1. 直连
2. 粘贴单条代理：HTTP、HTTPS 或 SOCKS5，可以整行粘贴代理
3. API 动态提取：每次申请开始前重新请求接口，解析 txt 或 JSON 返回

默认动态提取接口示例：

```text
https://api.cliproxy.io/white/api?region=Rand&num=1&time=10&format=n&type=txt
```

接口可以返回以下任一种格式，客户端会自动拆分地址、端口、账号和密码：

```text
HOST:PORT
HOST:PORT:USERNAME:PASSWORD
USERNAME:PASSWORD@HOST:PORT
socks5://USERNAME:PASSWORD@HOST:PORT
```

Cliproxy 白名单接口会自动锁定为 SOCKS5，旧版本保存的 HTTP 选项也会迁移，
无需逐项填写代理账号和密码。固定代理可以粘贴整行，或点击“从剪贴板导入”。

“提取并测试”会调用接口，完成真实 SOCKS5/HTTP 协议握手，并通过代理测试
连接 CTExcel。正式开始申请时仍会重新提取，避免使用测试阶段已经过期的
短效代理。日志只显示脱敏后的代理地址。

使用 Cliproxy 白名单提取接口时，需要先把运行 Windows 客户端的公网 IP
加入服务商白名单。客户端会在测试和申请前自动检测当前出口公网 IP，
显示在代理卡片中并提供复制按钮；若白名单、协议或有效期不正确，会在
创建客户之前停止，并在高对比度提示框中显示当前公网 IP 和具体错误，
避免产生新的空客户。

## 第一次设置

填写：

- 客户管理后台地址
- 客户端连接口令（与后台 `APP_PASSWORD` 相同）
- 固定姓、名
- 固定联系电话
- 固定中国收货地址

勾选保存连接口令时，Windows 版本使用当前 Windows 用户的 DPAPI
加密后保存到：

```text
%APPDATA%\CTExcelApplyClient\config.json
```

明文入口和口令不会写入仓库。

## 通信方式

客户端使用独立的限权接口，不依赖浏览器 Cookie：

1. `GET /api/ctexcel-client/status` 测试连接
2. `GET /api/ctexcel-client/customers/pending` 查询无手机号客户
3. `POST /api/ctexcel-client/customers` 复用待完成客户或创建新客户
4. `GET /api/ctexcel-client/customers/{id}/verification-code` 查询注册验证码
5. `POST /api/ctexcel-client/customers/{id}/payment-checkpoint` 回写订单号和付款金额
6. `POST /api/ctexcel-client/customers/{id}/order-info` 同步订单号和手机号

请求使用 HTTPS `Authorization: Bearer` 传递现有 `APP_PASSWORD`。这组接口只
开放连接检查、CTExcel 建档和对应邮箱接码；普通客户管理 API 仍受隐藏入口
与后台 Cookie 双重保护。错误连接口令同样执行 10 分钟 5 次的限速。

支付后，后台自动邮件同步内部复用：

```text
GET /api/customers/{id}/ctexcel-order-info
```

该接口的实际调用由后台定时同步任务完成。客户端只提交支付页显示的订单号和
付款金额，不读取成功页手机号。

## 中断恢复

后台创建客户成功后，即使网页流程中断，该客户和专属邮箱也已经保留。
再次点击“重试当前客户”时，客户端会先扫描全部无手机号客户的订单邮件，
再复用仍未产生订单的最新客户，不会因网页步骤失败重复建立空客户。

固定中国地址只用于 CTExcel 官网的智能地址填写，不写入客户管理的
“收货地址”字段。支付成功后立即结束当前单；连续模式直接进入下一单，
订单邮件由服务器后台继续同步。在客户管理详情页仍可查看邮箱、完整收件箱
并手动触发“扫描订单邮件”。

发送邮箱验证码前，客户端会先记录当前最新邮件 ID 和收件时间。发送后至少
等待 8 秒，只接受邮件 ID 已变化且收件时间属于本次请求的新验证码；上一次
尝试遗留的验证码会记录为“已忽略旧验证码”，不会自动填入。

结算页会在点击“使用优惠码”前核对输入框中的完整值并停留 2 秒。若网站
返回“优惠券不存在或已过期”，客户端会明确显示该优惠码已被网站拒绝，而
不是把网站自动清空输入框误报成未填写。

网页流程发生错误时，客户端会：

1. 将当前页面截图与 HTML 保存到：
   `%APPDATA%\CTExcelApplyClient\diagnostics`
2. 保存同时间戳的 `network.txt`，记录 CTExcel 接口状态和页面脚本错误。
3. 在可视模式下保留浏览器最多 180 秒，方便确认现场。
4. 在日志中显示具体未找到的控件名称与页面匹配数量。

客户端会自动关闭 CTExcel 页面的隐私设置遮罩，并兼容“实体 SIM 卡”标题
和说明文字处于同一个页面元素的结构。
