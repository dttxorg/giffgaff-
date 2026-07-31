import aiosqlite
import os
import sqlite3

DATABASE_PATH = os.path.join(os.path.dirname(__file__), "giffgaff.db")


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_type TEXT NOT NULL DEFAULT 'giffgaff',
                phone_number TEXT UNIQUE,
                email TEXT NOT NULL,
                shipping_address TEXT,
                shipping_status TEXT NOT NULL DEFAULT '未发货',
                courier_company TEXT,
                tracking_number TEXT,
                courier_order_code TEXT,
                courier_print_data TEXT,
                activation_date TEXT NOT NULL,
                moemail_id TEXT,
                moemail_address TEXT,
                share_link TEXT,
                is_moemail_auto INTEGER NOT NULL DEFAULT 0,
                sim_code_id INTEGER,
                sim_activation_code TEXT,
                initial_password TEXT,
                email_provider_id INTEGER,
                email_account_id TEXT,
                email_provider_domain TEXT,
                public_token TEXT,
                public_version INTEGER NOT NULL DEFAULT 1,
                phone_status TEXT NOT NULL DEFAULT '激活',
                payment_changed_at TEXT,
                payment_updated_at TEXT,
                payment_last_checked_at TEXT,
                activation_status TEXT NOT NULL DEFAULT '未开始',
                first_name TEXT,
                last_name TEXT,
                address TEXT,
                city TEXT,
                postcode TEXT,
                ctexcel_order_number TEXT,
                ctexcel_transaction_amount TEXT,
                ctexcel_referral_code TEXT,
                ctexcel_referral_link TEXT,
                ctexcel_last_checked_at TEXT,
                ctexcel_registration_confirmed_at TEXT,
                ctexcel_client_request_key TEXT,
                activation_error TEXT,
                activated_at TEXT,
                automation_lock_owner TEXT,
                automation_locked_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await _ensure_column(db, "customers", "product_type", "TEXT NOT NULL DEFAULT 'giffgaff'")
        await _ensure_column(db, "customers", "moemail_id", "TEXT")
        await _ensure_column(db, "customers", "moemail_address", "TEXT")
        await _ensure_column(db, "customers", "share_link", "TEXT")
        await _ensure_column(db, "customers", "is_moemail_auto", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "customers", "shipping_address", "TEXT")
        await _ensure_column(db, "customers", "shipping_status", "TEXT NOT NULL DEFAULT '未发货'")
        await _ensure_column(db, "customers", "courier_company", "TEXT")
        await _ensure_column(db, "customers", "tracking_number", "TEXT")
        await _ensure_column(db, "customers", "courier_order_code", "TEXT")
        await _ensure_column(db, "customers", "courier_print_data", "TEXT")
        await _ensure_column(db, "customers", "sim_code_id", "INTEGER")
        await _ensure_column(db, "customers", "sim_activation_code", "TEXT")
        await _ensure_column(db, "customers", "initial_password", "TEXT")
        await _ensure_column(db, "customers", "esim_raw_code", "TEXT")
        await _ensure_column(db, "customers", "email_provider_id", "INTEGER")
        await _ensure_column(db, "customers", "email_account_id", "TEXT")
        await _ensure_column(db, "customers", "email_provider_domain", "TEXT")
        await _ensure_column(db, "customers", "public_token", "TEXT")
        await _ensure_column(db, "customers", "first_name", "TEXT")
        await _ensure_column(db, "customers", "last_name", "TEXT")
        await _ensure_column(db, "customers", "address", "TEXT")
        await _ensure_column(db, "customers", "city", "TEXT")
        await _ensure_column(db, "customers", "postcode", "TEXT")
        await _ensure_column(db, "customers", "ctexcel_order_number", "TEXT")
        await _ensure_column(db, "customers", "ctexcel_transaction_amount", "TEXT")
        await _ensure_column(db, "customers", "ctexcel_referral_code", "TEXT")
        await _ensure_column(db, "customers", "ctexcel_referral_link", "TEXT")
        await _ensure_column(db, "customers", "ctexcel_last_checked_at", "TEXT")
        await _ensure_column(
            db,
            "customers",
            "ctexcel_registration_confirmed_at",
            "TEXT",
        )
        await _ensure_column(
            db,
            "customers",
            "ctexcel_client_request_key",
            "TEXT",
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_customers_ctexcel_client_request_key "
            "ON customers(ctexcel_client_request_key) "
            "WHERE ctexcel_client_request_key IS NOT NULL"
        )
        await _ensure_column(
            db, "customers", "public_version", "INTEGER NOT NULL DEFAULT 1"
        )
        await _ensure_column(
            db, "customers", "phone_status", "TEXT NOT NULL DEFAULT '激活'"
        )
        await _ensure_column(db, "customers", "payment_changed_at", "TEXT")
        await _ensure_column(db, "customers", "payment_updated_at", "TEXT")
        await _ensure_column(db, "customers", "payment_last_checked_at", "TEXT")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_customers_public_token "
            "ON customers(public_token) WHERE public_token IS NOT NULL"
        )
        await _ensure_column(db, "customers", "activation_status", "TEXT NOT NULL DEFAULT '未开始'")
        await _ensure_column(db, "customers", "activation_error", "TEXT")
        await _ensure_column(db, "customers", "activated_at", "TEXT")
        await _ensure_column(db, "customers", "automation_lock_owner", "TEXT")
        await _ensure_column(db, "customers", "automation_locked_at", "TEXT")
        await db.execute(
            """UPDATE customers
               SET product_type = 'giffgaff'
               WHERE product_type IS NULL
                  OR product_type NOT IN ('giffgaff', 'ctexcel')"""
        )
        await _ensure_activation_status_values(db)
        await _ensure_nullable_phone_number(db)
        # 旧版 phone_number NOT NULL 表重建时 SQLite 会随旧表删除索引，
        # 因此在迁移后再次确保两个唯一索引存在。
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_customers_public_token "
            "ON customers(public_token) WHERE public_token IS NOT NULL"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_customers_ctexcel_client_request_key "
            "ON customers(ctexcel_client_request_key) "
            "WHERE ctexcel_client_request_key IS NOT NULL"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sim_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT '未分配',
                customer_id INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                step TEXT,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sim_codes_status ON sim_codes(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_activation_logs_customer ON activation_logs(customer_id, created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # 桌面自动化注册功能已下线；启动时删除旧 Token，避免遗留凭证继续存在。
        await db.execute("DELETE FROM settings WHERE key = 'agent_api_token'")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS email_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                provider_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                domains_json TEXT,
                default_domain TEXT,
                disabled INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                last_error TEXT,
                last_error_at TEXT,
                last_jwt_token TEXT,
                last_jwt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await _ensure_column(db, "email_providers", "domains_json", "TEXT")
        await _ensure_column(db, "email_providers", "default_domain", "TEXT")
        await _ensure_column(db, "email_providers", "disabled", "INTEGER NOT NULL DEFAULT 0")
        await db.commit()


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in rows}
    if column not in existing_columns:
        try:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError as exc:
            # Uvicorn 多 Worker 会并发执行 startup：两个进程
            # 可能同时看到缺列，其中一个先完成 ALTER。
            if "duplicate column name" not in str(exc).lower():
                raise


async def _ensure_nullable_phone_number(db: aiosqlite.Connection):
    rows = await db.execute_fetchall("PRAGMA table_info(customers)")
    phone_column = next((row for row in rows if row[1] == "phone_number"), None)
    if not phone_column or phone_column[3] == 0:
        return

    await db.execute("ALTER TABLE customers RENAME TO customers_old")
    await db.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_type TEXT NOT NULL DEFAULT 'giffgaff',
            phone_number TEXT UNIQUE,
            email TEXT NOT NULL,
            shipping_address TEXT,
            shipping_status TEXT NOT NULL DEFAULT '未发货',
            courier_company TEXT,
            tracking_number TEXT,
            courier_order_code TEXT,
            courier_print_data TEXT,
            activation_date TEXT NOT NULL,
            moemail_id TEXT,
            moemail_address TEXT,
            share_link TEXT,
            is_moemail_auto INTEGER NOT NULL DEFAULT 0,
            sim_code_id INTEGER,
            sim_activation_code TEXT,
            initial_password TEXT,
            esim_raw_code TEXT,
            email_provider_id INTEGER,
            email_account_id TEXT,
            email_provider_domain TEXT,
            public_token TEXT,
            public_version INTEGER NOT NULL DEFAULT 1,
            phone_status TEXT NOT NULL DEFAULT '激活',
            payment_changed_at TEXT,
            payment_updated_at TEXT,
            payment_last_checked_at TEXT,
            activation_status TEXT NOT NULL DEFAULT '未开始',
            first_name TEXT,
            last_name TEXT,
            address TEXT,
            city TEXT,
            postcode TEXT,
            ctexcel_order_number TEXT,
            ctexcel_transaction_amount TEXT,
            ctexcel_referral_code TEXT,
            ctexcel_referral_link TEXT,
            ctexcel_last_checked_at TEXT,
            ctexcel_registration_confirmed_at TEXT,
            ctexcel_client_request_key TEXT,
            activation_error TEXT,
            activated_at TEXT,
            automation_lock_owner TEXT,
            automation_locked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # init_db 已先补齐所有当前字段；按字段名整体复制，避免表重建时遗失
    # provider、英国地址、CTExcel 订单或公开二维码版本等后加数据。
    column_names = [row[1] for row in rows]
    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    await db.execute(
        f"""INSERT INTO customers ({quoted_columns})
            SELECT {quoted_columns} FROM customers_old"""
    )
    await db.execute("DROP TABLE customers_old")


async def _ensure_activation_status_values(db: aiosqlite.Connection):
    # 旧版「等待客户端领取」属于已下线的桌面自动化流程，统一迁移到人工状态。
    await db.execute("""
        UPDATE customers
        SET activation_status = '已分配激活码',
            automation_lock_owner = NULL,
            automation_locked_at = NULL
        WHERE activation_status = '等待客户端领取'
    """)
    await db.execute("""
        UPDATE customers
        SET automation_lock_owner = NULL,
            automation_locked_at = NULL
        WHERE automation_lock_owner IS NOT NULL
           OR automation_locked_at IS NOT NULL
    """)
    await db.execute("""
        UPDATE customers
        SET activation_status = '未开始'
        WHERE activation_status IS NULL
           OR activation_status = ''
           OR activation_status NOT IN (
               '未开始', '已分配激活码', '激活中',
               '等待人工支付', '等待转 eSIM', '已完成', '失败'
           )
    """)
