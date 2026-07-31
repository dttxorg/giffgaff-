from __future__ import annotations

from typing import Any, Optional

import httpx


class ApiError(RuntimeError):
    pass


class AdminApi:
    """访问服务器上仅开放 CTExcel 建档与接码能力的限权 API。"""

    def __init__(
        self,
        server_url: str,
        app_password: str = "",
        *,
        timeout: float = 25.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.server_url = server_url.strip().rstrip("/")
        self.app_password = app_password.strip()
        headers = {
            "User-Agent": "CTExcelApplyClient/2.3.4",
            "Accept": "application/json",
        }
        if self.app_password:
            headers["Authorization"] = f"Bearer {self.app_password}"
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers=headers,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AdminApi":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _url(self, path: str) -> str:
        if not self.server_url:
            raise ApiError("请填写服务器地址")
        if not self.server_url.startswith(("https://", "http://")):
            raise ApiError("服务器地址需要以 https:// 开头")
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
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.app_password:
            raise ApiError("请填写客户端连接口令")
        try:
            response = self.client.request(
                method,
                self._url(path),
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"连接服务器失败：{exc}") from exc
        if response.status_code >= 400:
            detail = self._detail(response)
            if response.status_code == 401:
                detail = detail or "客户端连接口令错误"
            elif response.status_code == 404:
                detail = "服务器尚未启用 CTExcel 客户端 API"
            elif response.status_code == 429:
                detail = detail or "连接失败次数过多，请稍后再试"
            raise ApiError(detail or f"服务器返回 HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("服务器接口返回格式错误") from exc

    def connect(self) -> dict[str, Any]:
        status = self._request("GET", "/api/ctexcel-client/status")
        if not isinstance(status, dict) or not status.get("ok"):
            raise ApiError("CTExcel 客户端 API 状态异常")
        return status

    def pending_customers(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/ctexcel-client/customers/pending",
        )
        if not isinstance(data, dict) or not isinstance(data.get("customers"), list):
            raise ApiError("待完成 CTExcel 客户列表格式错误")
        return [
            customer
            for customer in data["customers"]
            if isinstance(customer, dict)
        ]

    def create_ctexcel_customer(
        self,
        *,
        allow_new_after_checkpoint: bool = False,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/api/ctexcel-client/customers",
            json_body={
                "reuse_pending": True,
                "allow_new_after_checkpoint": bool(
                    allow_new_after_checkpoint
                ),
            },
        )
        if not isinstance(data, dict):
            raise ApiError("准备 CTExcel 客户返回格式错误")
        customer_id = data.get("customer_id")
        email = str(data.get("email") or "").strip()
        if not customer_id or not email:
            raise ApiError("客户已准备，但没有取得专属邮箱")
        return data

    def verification_code(self, customer_id: int) -> dict[str, Any]:
        data = self._request(
            "GET",
            (
                "/api/ctexcel-client/customers/"
                f"{int(customer_id)}/verification-code"
            ),
        )
        if not isinstance(data, dict):
            raise ApiError("验证码接口返回格式错误")
        return data

    def sync_order_info(self, customer_id: int) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/api/ctexcel-client/customers/{int(customer_id)}/order-info",
        )
        if not isinstance(data, dict):
            raise ApiError("CTExcel 订单资料接口返回格式错误")
        return data

    def save_payment_checkpoint(
        self,
        customer_id: int,
        *,
        order_number: str,
        transaction_amount: str,
        phone_number: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "order_number": str(order_number or "").strip() or None,
            "transaction_amount": str(transaction_amount or "").strip(),
        }
        if str(phone_number or "").strip():
            body["phone_number"] = str(phone_number).strip()
        data = self._request(
            "POST",
            (
                "/api/ctexcel-client/customers/"
                f"{int(customer_id)}/payment-checkpoint"
            ),
            json_body=body,
        )
        if not isinstance(data, dict) or not data.get("ok"):
            raise ApiError("CTExcel 付款金额回写接口返回格式错误")
        return data
