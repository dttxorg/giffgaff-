from __future__ import annotations

from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import httpx


class ApiError(RuntimeError):
    pass


class AdminApi:
    """使用隐藏入口 Cookie + APP_PASSWORD 会话访问客户管理 API。"""

    def __init__(
        self,
        server_url: str,
        admin_entry_path: str = "",
        app_password: str = "",
        *,
        timeout: float = 25.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.server_url = server_url.strip().rstrip("/")
        self.admin_entry_path = self._normalize_entry_path(admin_entry_path)
        self.app_password = app_password
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": "CTExcelApplyClient/1.0"},
        )

    @staticmethod
    def _normalize_entry_path(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        return "/" + value.strip("/")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AdminApi":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _url(self, path: str) -> str:
        if not self.server_url:
            raise ApiError("请先填写客户管理系统地址")
        return f"{self.server_url}{path}"

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return ""
        return str(data.get("detail") or "") if isinstance(data, dict) else ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            response = self.client.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"连接客户管理系统失败：{exc}") from exc
        if response.status_code >= 400:
            detail = self._detail(response)
            if response.status_code == 404 and not detail:
                detail = "隐藏入口未生效或入口路径错误"
            raise ApiError(detail or f"接口返回 HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("客户管理接口返回格式错误") from exc

    def connect(self) -> dict[str, Any]:
        if self.admin_entry_path:
            try:
                response = self.client.get(self._url(self.admin_entry_path))
            except httpx.HTTPError as exc:
                raise ApiError(f"访问客户管理隐藏入口失败：{exc}") from exc
            if response.status_code >= 400:
                detail = self._detail(response)
                raise ApiError(
                    detail
                    or (
                        "隐藏入口返回 404，请检查完整随机路径"
                        if response.status_code == 404
                        else f"隐藏入口返回 HTTP {response.status_code}"
                    )
                )
        status = self._request("GET", "/api/auth/status")
        if not isinstance(status, dict):
            raise ApiError("认证状态返回格式错误")
        if status.get("auth_required") and not status.get("authenticated"):
            if not self.app_password:
                raise ApiError("请填写客户管理系统访问口令")
            self._request(
                "POST",
                "/api/auth/login",
                json_body={"password": self.app_password},
            )
            status = self._request("GET", "/api/auth/status")
        if status.get("auth_required") and not status.get("authenticated"):
            raise ApiError("客户管理系统登录状态未建立")
        return status

    def list_ctexcel_customers(self, search: str = "") -> list[dict[str, Any]]:
        data = self._request("GET", "/api/customers", params={"search": search})
        if not isinstance(data, list):
            raise ApiError("客户列表返回格式错误")
        return [
            item
            for item in data
            if isinstance(item, dict) and item.get("product_type") == "ctexcel"
        ]

    def get_customer(self, customer_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/api/customers/{int(customer_id)}")
        if not isinstance(data, dict):
            raise ApiError("客户资料返回格式错误")
        if data.get("product_type") != "ctexcel":
            raise ApiError("该客户不是 CTExcel 模式")
        return data

    def create_ctexcel_customer(self, shipping_address: str) -> dict[str, Any]:
        payload = {
            "product_type": "ctexcel",
            "phone_number": None,
            "email": "",
            "shipping_address": shipping_address.strip() or None,
            "phone_status": "激活",
            "activation_date": date.today().isoformat(),
            "use_sim_code": False,
        }
        data = self._request("POST", "/api/customers", json_body=payload)
        if not isinstance(data, dict):
            raise ApiError("新建 CTExcel 客户返回格式错误")
        customer_id = data.get("customer_id")
        email = str(data.get("email") or "").strip()
        if not customer_id or not email:
            raise ApiError("客户已创建，但没有取得专属邮箱")
        return data

    def verification_code(self, customer_id: int) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/customers/{int(customer_id)}/verification-code",
        )
        if not isinstance(data, dict):
            raise ApiError("验证码接口返回格式错误")
        return data

    def sync_order_email(self, customer_id: int) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/customers/{int(customer_id)}/ctexcel-order-info",
        )
        if not isinstance(data, dict):
            raise ApiError("订单邮件同步接口返回格式错误")
        return data

    def find_customer_by_email(self, email: str) -> Optional[dict[str, Any]]:
        email = (email or "").strip()
        if not email:
            return None
        rows = self.list_ctexcel_customers(search=email)
        return next(
            (
                row
                for row in rows
                if str(row.get("email") or "").lower() == email.lower()
            ),
            None,
        )

    def public_customer_path(self, customer_id: int) -> str:
        return f"/api/customers/{quote(str(int(customer_id)), safe='')}"
