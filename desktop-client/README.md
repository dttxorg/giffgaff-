# CTExcel 自动申请客户端（Windows）

该客户端只负责 CTExcel 新客户的申请购买流程：

1. 使用独立的 CTExcel 限权 API 和 `APP_PASSWORD` 建立连接。
2. 在客户管理列表中新建一个 `ctexcel` 客户。
3. 客户管理系统通过 MoEmail / CloudMail 自动生成专属邮箱，并返回
   `customer_id + email`。
4. 客户端用该邮箱完成 CTExcel 注册、邮箱验证码、地址、优惠码和微信支付。
5. 支付成功后客户端流程结束。
6. 客户管理系统后台继续扫描该专属邮箱，把订单号、手机号码、交易金额、
   推荐码和推荐链接写入同一客户记录。

客户端不访问隐藏管理入口、不使用旧的 Agent Token，也不直接回写订单字段。客户和订单通过
**同一个专属邮箱**自然关联。

## 固定流程

- 套餐：50GB，£11.9/30天
- 实体 SIM
- 免费随机号码
- 1个月、1张
- 自动续订关闭
- 寄送国家：中国
- 地址：使用 CTExcel「智能填写」
- 推荐码：`NTKWJX`
- 优惠码：`DEAL50OFF`
- 优惠后价格强制校验：`£5.95`
- 支付方式：微信
- 人工扫码支付

姓名、联系电话和中国收货地址在客户端设置里配置一次。客户邮箱始终由
客户管理系统新建客户时生成。

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
5. `POST /api/ctexcel-client/customers/{id}/order-info` 同步订单号和手机号

请求使用 HTTPS `Authorization: Bearer` 传递现有 `APP_PASSWORD`。这组接口只
开放连接检查、CTExcel 建档和对应邮箱接码；普通客户管理 API 仍受隐藏入口
与后台 Cookie 双重保护。错误连接口令同样执行 10 分钟 5 次的限速。

支付后，后台自动邮件同步内部复用：

```text
GET /api/customers/{id}/ctexcel-order-info
```

该接口的实际调用由后台定时同步任务完成，客户端不提交订单号或手机号。

## 中断恢复

后台创建客户成功后，即使网页流程中断，该客户和专属邮箱也已经保留。
再次点击“重试当前客户”时，客户端会先扫描全部无手机号客户的订单邮件，
再复用仍未产生订单的最新客户，不会因网页步骤失败重复建立空客户。

固定中国地址只用于 CTExcel 官网的智能地址填写，不写入客户管理的
“收货地址”字段。支付成功后客户端会立即轮询订单邮件同步手机号；在客户
管理详情页仍可查看邮箱、完整收件箱并手动触发“扫描订单邮件”。

网页流程发生错误时，客户端会：

1. 将当前页面截图与 HTML 保存到：
   `%APPDATA%\CTExcelApplyClient\diagnostics`
2. 在可视模式下保留浏览器最多 180 秒，方便确认现场。
3. 在日志中显示具体未找到的控件名称与页面匹配数量。

客户端会自动关闭 CTExcel 页面的隐私设置遮罩，并兼容“实体 SIM 卡”标题
和说明文字处于同一个页面元素的结构。
