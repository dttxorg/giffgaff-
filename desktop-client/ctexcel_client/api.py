from __future__ import annotations

import time
from typing import Any, Callable, Optional

import httpx


class ApiError(RuntimeError):
    pass


TRANSIENT_STATUS_CODES = {
    408,
    425,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
    525,
    526,
}
DEFAULT_RETRY_DELAYS = (
    2.0,
    4.0,
    8.0,
    15.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
    30.0,
)


class AdminApi:
    """访问服务器上仅开放 CTExcel 建档与接码能力的限权 API。"""

    def __init__(
        self,
        server_url: str,
        app_password: str = "",
        *,
        timeout: float = 25.0,
        transport: Optional[httpx.BaseTransport] = None,
        retry_callback: Optional[Callable[[str], None]] = None,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.server_url = server_url.strip().rstrip("/")
        self.app_password = app_password.strip()
        headers = {
            "User-Agent": "CTExcelApplyClient/2.5.8",
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
        self.retry_callback = retry_callback
        self.retry_delays = tuple(
            max(0.0, float(delay))
            for delay in retry_delays
        )
        self.sleep = sleep

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

    def _retry(
        self,
        *,
        attempt: int,
        reason: str,
    ) -> bool:
        if attempt >= len(self.retry_delays):
            return False
        delay = self.retry_delays[attempt]
        if self.retry_callback:
            self.retry_callback(
                f"客户管理临时连接异常（{reason}），"
                f"{delay:g} 秒后自动重试 "
                f"{attempt + 1}/{len(self.retry_delays)}"
            )
        self.sleep(delay)
        return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.app_password:
            raise ApiError("请填写客户端连接口令")
        url = self._url(path)
        for attempt in range(len(self.retry_delays) + 1):
            try:
                response = self.client.request(
                    method,
                    url,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                if self._retry(
                    attempt=attempt,
                    reason=type(exc).__name__,
                ):
                    continue
                raise ApiError(f"连接服务器失败：{exc}") from exc

            if (
                response.status_code in TRANSIENT_STATUS_CODES
                or 500 <= response.status_code <= 599
            ):
                if self._retry(
                    attempt=attempt,
                    reason=f"HTTP {response.status_code}",
                ):
                    continue

            if response.status_code >= 400:
                detail = self._detail(response)
                if response.status_code == 401:
                    detail = detail or "客户端连接口令错误"
                elif response.status_code == 404:
                    detail = "服务器尚未启用 CTExcel 客户端 API"
                elif response.status_code == 429:
                    detail = detail or "连接失败次数过多，请稍后再试"
                raise ApiError(
                    detail or f"服务器返回 HTTP {response.status_code}"
                )
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                if self._retry(
                    attempt=attempt,
                    reason="响应格式不完整",
                ):
                    continue
                raise ApiError("服务器接口返回格式错误") from exc
        raise ApiError("客户管理连接重试结束")

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
        reuse_pending: bool = True,
        allow_new_after_checkpoint: bool = False,
        resume_customer_id: Optional[int] = None,
        request_key: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "reuse_pending": bool(reuse_pending),
            "allow_new_after_checkpoint": bool(
                allow_new_after_checkpoint
            ),
        }
        if str(request_key or "").strip():
            body["request_key"] = str(request_key).strip()
        if resume_customer_id is not None:
            body["resume_customer_id"] = int(resume_customer_id)
        data = self._request(
            "POST",
            "/api/ctexcel-client/customers",
            json_body=body,
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
        payment_succeeded: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "order_number": str(order_number or "").strip() or None,
            "transaction_amount": str(transaction_amount or "").strip(),
        }
        if str(phone_number or "").strip():
            body["phone_number"] = str(phone_number).strip()
        if payment_succeeded:
            body["payment_succeeded"] = True
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

    def release_ctexcel_customer(
        self,
        customer_id: int,
        *,
        request_key: str,
    ) -> bool:
        data = self._request(
            "POST",
            f"/api/ctexcel-client/customers/{int(customer_id)}/release",
            json_body={"request_key": str(request_key).strip()},
        )
        if not isinstance(data, dict) or not data.get("ok"):
            raise ApiError("释放 CTExcel 客户租约返回格式错误")
        return bool(data.get("released"))
