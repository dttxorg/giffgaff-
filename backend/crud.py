import secrets

import aiosqlite
from models import CustomerCreate, CustomerUpdate
from database import DATABASE_PATH
from typing import Optional


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


async def fetch_one(db: aiosqlite.Connection, query: str, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def get_all_customers():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM customers ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]


async def search_customers(query: str):
    """模糊搜索：手机号 / 快递单号 / 快递公司 / 快递订单号 / 邮箱 任一字段含子串即匹配。
    大小写不敏感，按 created_at DESC 排序。空串返回全部。"""
    q = (query or "").strip().lower()
    if not q:
        return await get_all_customers()
    pattern = f"%{q}%"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT * FROM customers
               WHERE LOWER(COALESCE(phone_number, '')) LIKE ?
                  OR LOWER(COALESCE(tracking_number, '')) LIKE ?
                  OR LOWER(COALESCE(courier_company, '')) LIKE ?
                  OR LOWER(COALESCE(courier_order_code, '')) LIKE ?
                  OR LOWER(COALESCE(ctexcel_order_number, '')) LIKE ?
                  OR LOWER(COALESCE(ctexcel_login_account, '')) LIKE ?
                  OR LOWER(COALESCE(email, '')) LIKE ?
               ORDER BY created_at DESC""",
            (pattern, pattern, pattern, pattern, pattern, pattern, pattern),
        )
        return [dict(r) for r in rows] 


async def get_customer(customer_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        )
        return dict(row) if row else None


async def create_customer(data: CustomerCreate):
    phone_number = normalize_optional_text(data.phone_number)
    shipping_address = normalize_optional_text(data.shipping_address)
    courier_company = normalize_optional_text(data.courier_company)
    tracking_number = normalize_optional_text(data.tracking_number)
    courier_order_code = normalize_optional_text(data.courier_order_code)
    courier_print_data = normalize_optional_text(data.courier_print_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO customers
               (product_type, phone_number, email, shipping_address, courier_company,
                tracking_number, courier_order_code, courier_print_data, activation_date,
                public_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.product_type, phone_number, data.email, shipping_address, courier_company, tracking_number,
             courier_order_code, courier_print_data, data.activation_date.isoformat(),
             secrets.token_urlsafe(32)),
        )
        await db.commit()
        return cursor.lastrowid


async def update_customer(customer_id: int, data: CustomerUpdate):
    fields, values = [], []
    if data.phone_number is not None:
        fields.append("phone_number = ?"); values.append(normalize_optional_text(data.phone_number))
    if data.email is not None:
        fields.append("email = ?"); values.append(data.email)
    if data.shipping_address is not None:
        fields.append("shipping_address = ?"); values.append(normalize_optional_text(data.shipping_address))
    if data.phone_status is not None:
        fields.append("phone_status = ?"); values.append(data.phone_status)
    if data.courier_company is not None:
        fields.append("courier_company = ?"); values.append(normalize_optional_text(data.courier_company))
    if data.tracking_number is not None:
        fields.append("tracking_number = ?"); values.append(normalize_optional_text(data.tracking_number))
    if data.courier_order_code is not None:
        fields.append("courier_order_code = ?"); values.append(normalize_optional_text(data.courier_order_code))
    if data.courier_print_data is not None:
        fields.append("courier_print_data = ?"); values.append(normalize_optional_text(data.courier_print_data))
    if data.activation_date is not None:
        fields.append("activation_date = ?"); values.append(data.activation_date.isoformat())
    if data.activation_status is not None:
        fields.append("activation_status = ?"); values.append(data.activation_status)
    if data.activation_error is not None:
        fields.append("activation_error = ?"); values.append(normalize_optional_text(data.activation_error))
    if data.first_name is not None:
        fields.append("first_name = ?"); values.append(data.first_name)
    if data.last_name is not None:
        fields.append("last_name = ?"); values.append(data.last_name)
    if data.address is not None:
        fields.append("address = ?"); values.append(data.address)
    if data.city is not None:
        fields.append("city = ?"); values.append(data.city)
    if data.postcode is not None:
        fields.append("postcode = ?"); values.append(data.postcode)
    if data.ctexcel_order_number is not None:
        fields.append("ctexcel_order_number = ?"); values.append(normalize_optional_text(data.ctexcel_order_number))
    if data.ctexcel_transaction_amount is not None:
        fields.append("ctexcel_transaction_amount = ?"); values.append(normalize_optional_text(data.ctexcel_transaction_amount))
    if data.ctexcel_referral_code is not None:
        fields.append("ctexcel_referral_code = ?"); values.append(normalize_optional_text(data.ctexcel_referral_code))
    if data.ctexcel_referral_link is not None:
        fields.append("ctexcel_referral_link = ?"); values.append(normalize_optional_text(data.ctexcel_referral_link))
    if data.ctexcel_login_account is not None:
        fields.append("ctexcel_login_account = ?"); values.append(normalize_optional_text(data.ctexcel_login_account))
    if data.ctexcel_initial_password is not None:
        fields.append("ctexcel_initial_password = ?"); values.append(normalize_optional_text(data.ctexcel_initial_password))
    if not fields:
        return True

    public_updates = {}
    for field in (
        "phone_number",
        "ctexcel_order_number",
        "ctexcel_transaction_amount",
        "ctexcel_referral_code",
        "ctexcel_referral_link",
        "ctexcel_login_account",
        "ctexcel_initial_password",
    ):
        value = getattr(data, field)
        if value is not None:
            public_updates[field] = normalize_optional_text(value)
    if data.email is not None:
        public_updates["email"] = data.email

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await fetch_one(
                db,
                "SELECT * FROM customers WHERE id = ?",
                (customer_id,),
            )
            if not current:
                await db.rollback()
                return False
            if (
                (current["product_type"] or "giffgaff") == "ctexcel"
                and any(current[field] != value for field, value in public_updates.items())
            ):
                # 公开字段变化只让 Worker 换缓存 key；客户二维码 token 保持不变。
                fields.append(
                    "public_version = COALESCE(public_version, 1) + 1"
                )
            cursor = await db.execute(
                f"UPDATE customers SET {', '.join(fields)} WHERE id = ?",
                (*values, customer_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def delete_customer(customer_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        await db.commit()
        return True


async def update_customer_moemail(customer_id: int, moemail_id: str,
                                    moemail_address: str, share_link: str,
                                    is_moemail_auto: bool,
                                    email_provider_id: Optional[int] = None,
                                    email_provider_domain: Optional[str] = None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE customers SET
               email = ?, moemail_id = ?, moemail_address = ?, share_link = ?, is_moemail_auto = ?,
               email_provider_id = ?, email_account_id = ?, email_provider_domain = ?
               WHERE id = ?""",
            (moemail_address, moemail_id, moemail_address, share_link,
             1 if is_moemail_auto else 0, email_provider_id, moemail_id,
             email_provider_domain, customer_id),
        )
        await db.commit()


# ── 系统设置 ──

async def get_settings() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}


# ── 公开页面（扫码后展示）──

async def get_public_email(token: str) -> Optional[str]:
    """仅按 token 查邮箱。绝不返回其它客户字段，避免越权泄露。"""
    if not token or len(token) > 128:
        return None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            "SELECT email FROM customers WHERE public_token = ?",
            (token,),
        )
        if not row:
            return None
        email = row["email"]
        return email if (email and email.strip()) else None


async def get_public_card(token: str) -> Optional[dict]:
    """扫码公开页面所需的客户字段：用于渲染 email + 替换 markdown 里的 {var}。
    仍然只返回「已配置好」的客户记录——没有有效 token 返 None。"""
    if not token or len(token) > 128:
        return None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            """SELECT email, public_version, product_type, phone_number, moemail_address,
                      first_name, last_name, address, city, postcode,
                      sim_activation_code, initial_password, share_link,
                      activation_date, phone_status, shipping_address,
                      ctexcel_order_number, ctexcel_transaction_amount,
                      ctexcel_referral_code, ctexcel_referral_link,
                      ctexcel_login_account, ctexcel_initial_password
               FROM customers WHERE public_token = ?""",
            (token,),
        )
        if not row:
            return None
        email = row["email"]
        if not email or not email.strip():
            return None
        return {
            "email": email,
            "product_type": row["product_type"] or "giffgaff",
            "public_version": int(row["public_version"] or 1),
            "phone_number": row["phone_number"],
            "moemail_address": row["moemail_address"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "address": row["address"],
            "city": row["city"],
            "postcode": row["postcode"],
            "sim_activation_code": row["sim_activation_code"],
            "initial_password": row["initial_password"],
            "share_link": row["share_link"],
            "activation_date": row["activation_date"],
            "phone_status": row["phone_status"],
            "shipping_address": row["shipping_address"],
            "ctexcel_order_number": row["ctexcel_order_number"],
            "ctexcel_transaction_amount": row["ctexcel_transaction_amount"],
            "ctexcel_referral_code": row["ctexcel_referral_code"],
            "ctexcel_referral_link": row["ctexcel_referral_link"],
            "ctexcel_login_account": row["ctexcel_login_account"],
            "ctexcel_initial_password": row["ctexcel_initial_password"],
        }


async def get_public_version(token: str) -> Optional[int]:
    """仅返回 public_version（不返 email），给 Worker 做版本化缓存 key 用。
    即使 email 尚未配置，只要 token 存在就返回 version。"""
    if not token or len(token) > 128:
        return None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        row = await fetch_one(
            db,
            "SELECT public_version FROM customers WHERE public_token = ?",
            (token,),
        )
        if not row:
            return None
        return int(row[0] or 1)


async def get_public_version_info(token: str) -> Optional[dict]:
    """返回公开页缓存版本和产品类型，不返回邮箱或客户资料。"""
    if not token or len(token) > 128:
        return None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            """SELECT public_version, product_type
               FROM customers WHERE public_token = ?""",
            (token,),
        )
        if not row:
            return None
        return {
            "public_version": int(row["public_version"] or 1),
            "product_type": row["product_type"] or "giffgaff",
        }


async def regenerate_public_link(customer_id: int) -> Optional[dict]:
    """旋转 public_token、public_version +1。
    旧 token 立刻在 DB 失效（Worker 再回调会拿到 404）。
    返回 {public_token, public_version}；客户不存在时返回 None。"""
    new_token = secrets.token_urlsafe(32)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            "SELECT public_version FROM customers WHERE id = ?",
            (customer_id,),
        )
        if not row:
            return None
        new_version = int(row["public_version"] or 1) + 1
        await db.execute(
            "UPDATE customers SET public_token = ?, public_version = ? WHERE id = ?",
            (new_token, new_version, customer_id),
        )
        await db.commit()
        return {"public_token": new_token, "public_version": new_version}


async def bump_all_public_versions() -> int:
    """公开联系方式页面内容/设计改变时，仅提升缓存版本，不旋转二维码 token。"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """UPDATE customers
               SET public_version = COALESCE(public_version, 1) + 1
               WHERE public_token IS NOT NULL AND public_token != ''"""
        )
        await db.commit()
        return cursor.rowcount


async def ensure_public_link(customer_id: int) -> Optional[dict]:
    """按需为旧客户补一个公开链接，但绝不旋转已经存在的 token。

    只在用户实际预览/打印含公开页二维码的标签时调用，因此不会批量补齐
    存量客户。并发调用时用条件 UPDATE，避免同一客户生成多个生效 token。
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await fetch_one(
            db,
            "SELECT public_token, public_version FROM customers WHERE id = ?",
            (customer_id,),
        )
        if not row:
            return None
        if row["public_token"]:
            return {
                "public_token": row["public_token"],
                "public_version": int(row["public_version"] or 1),
            }

        new_token = secrets.token_urlsafe(32)
        await db.execute(
            """UPDATE customers
               SET public_token = ?
               WHERE id = ? AND (public_token IS NULL OR public_token = '')""",
            (new_token, customer_id),
        )
        await db.commit()

        # 如果另一个请求抢先写入，返回数据库中最终生效的 token。
        current = await fetch_one(
            db,
            "SELECT public_token, public_version FROM customers WHERE id = ?",
            (customer_id,),
        )
        return {
            "public_token": current["public_token"],
            "public_version": int(current["public_version"] or 1),
        }


async def save_payment_check_result(
    customer_id: int,
    changed_at: Optional[str],
    updated_at: Optional[str],
    checked_at: Optional[str],
) -> bool:
    """保存「查解绑」结果到 DB，供首页列表展示。
    changed_at / updated_at 来自最新一封「changed」/「updated」邮件的 received_at。
    checked_at 是查询发生时间（即使没找到任何邮件也会写）。
    客户不存在时返回 False。"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """UPDATE customers
               SET payment_changed_at = ?, payment_updated_at = ?, payment_last_checked_at = ?
               WHERE id = ?""",
            (changed_at, updated_at, checked_at, customer_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_ctexcel_order_info(
    customer_id: int,
    *,
    phone_number: Optional[str],
    order_number: Optional[str],
    transaction_amount: Optional[str],
    referral_code: Optional[str],
    referral_link: Optional[str],
    esim_raw_code: Optional[str],
    login_account: Optional[str],
    initial_password: Optional[str],
    registration_confirmed_at: Optional[str],
    checked_at: str,
) -> bool:
    """保存 CTExcel 订单/eSIM 邮件中解析出的资料；空字段保留现有值。"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        incoming = {
            "phone_number": normalize_optional_text(phone_number),
            "ctexcel_order_number": normalize_optional_text(order_number),
            "ctexcel_transaction_amount": normalize_optional_text(transaction_amount),
            "ctexcel_referral_code": normalize_optional_text(referral_code),
            "ctexcel_referral_link": normalize_optional_text(referral_link),
            "esim_raw_code": normalize_optional_text(esim_raw_code),
            "ctexcel_login_account": normalize_optional_text(login_account),
            "ctexcel_initial_password": normalize_optional_text(initial_password),
        }
        try:
            # 手动扫描和后台扫描可能重叠；串行比较后只为一次真实变化提升一次版本。
            await db.execute("BEGIN IMMEDIATE")
            current = await fetch_one(
                db,
                "SELECT * FROM customers WHERE id = ? AND product_type = 'ctexcel'",
                (customer_id,),
            )
            if not current:
                await db.rollback()
                return False
            public_changed = any(
                value is not None and current[field] != value
                for field, value in incoming.items()
            )
            cursor = await db.execute(
                """UPDATE customers
                   SET phone_number = COALESCE(?, phone_number),
                       ctexcel_order_number = COALESCE(?, ctexcel_order_number),
                       ctexcel_transaction_amount = COALESCE(?, ctexcel_transaction_amount),
                       ctexcel_referral_code = COALESCE(?, ctexcel_referral_code),
                       ctexcel_referral_link = COALESCE(?, ctexcel_referral_link),
                       esim_raw_code = COALESCE(?, esim_raw_code),
                       ctexcel_login_account = COALESCE(?, ctexcel_login_account),
                       ctexcel_initial_password = COALESCE(?, ctexcel_initial_password),
                       ctexcel_registration_confirmed_at =
                           COALESCE(?, ctexcel_registration_confirmed_at),
                       ctexcel_last_checked_at = ?,
                       public_version = COALESCE(public_version, 1) + ?
                   WHERE id = ? AND product_type = 'ctexcel'""",
                (
                    *incoming.values(),
                    normalize_optional_text(registration_confirmed_at),
                    checked_at,
                    1 if public_changed else 0,
                    customer_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def save_ctexcel_payment_checkpoint(
    customer_id: int,
    *,
    order_number: Optional[str],
    transaction_amount: str,
    phone_number: Optional[str] = None,
    payment_succeeded_at: Optional[str] = None,
) -> bool:
    """保存支付页订单资料；成功页可同时补全手机号码。"""
    incoming = {
        "ctexcel_order_number": normalize_optional_text(order_number),
        "ctexcel_transaction_amount": normalize_optional_text(transaction_amount),
        "phone_number": normalize_optional_text(phone_number),
    }
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await fetch_one(
                db,
                "SELECT * FROM customers WHERE id = ? AND product_type = 'ctexcel'",
                (customer_id,),
            )
            if not current:
                await db.rollback()
                return False
            public_changed = any(
                value is not None and current[field] != value
                for field, value in incoming.items()
            )
            cursor = await db.execute(
                """UPDATE customers
                   SET ctexcel_order_number = COALESCE(?, ctexcel_order_number),
                       ctexcel_transaction_amount = ?,
                       phone_number = COALESCE(?, phone_number),
                       ctexcel_payment_succeeded_at =
                           COALESCE(?, ctexcel_payment_succeeded_at),
                       public_version = COALESCE(public_version, 1) + ?
                   WHERE id = ? AND product_type = 'ctexcel'""",
                (
                    incoming["ctexcel_order_number"],
                    incoming["ctexcel_transaction_amount"],
                    incoming["phone_number"],
                    normalize_optional_text(payment_succeeded_at),
                    1 if public_changed else 0,
                    customer_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise



async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        await db.commit()


async def regenerate_identity(customer_id: int) -> Optional[dict]:
    """重新随机生成 first_name/last_name/address/city/postcode 并落库。
    返回新的 {first_name, last_name, address, city, postcode}；客户不存在时返回 None。"""
    from uk_random import generate_random_identity
    identity = generate_random_identity()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """UPDATE customers
               SET first_name = ?, last_name = ?, address = ?, city = ?, postcode = ?
               WHERE id = ?""",
            (identity["first_name"], identity["last_name"], identity["address"],
             identity["city"], identity["postcode"], customer_id),
        )
        await db.commit()
        return identity if cursor.rowcount > 0 else None
