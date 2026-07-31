from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import TelegramConfig


class TelegramError(RuntimeError):
    pass


def validate_telegram_config(config: TelegramConfig) -> None:
    token = str(config.bot_token or "").strip()
    chat_id = str(config.chat_id or "").strip()
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", token):
        raise TelegramError("Telegram Bot Token 格式错误")
    if not re.fullmatch(r"(?:-?\d+|@[A-Za-z0-9_]{5,})", chat_id):
        raise TelegramError("Telegram Chat ID 格式错误")


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        proxy: dict[str, str] | None = None,
    ):
        validate_telegram_config(config)
        self.token = config.bot_token.strip()
        self.chat_id = config.chat_id.strip()
        proxy_url = self._proxy_url(proxy)
        self.client = httpx.Client(
            timeout=timeout,
            transport=transport,
            proxy=proxy_url,
            headers={"User-Agent": "CTExcelApplyClient/2.4.2"},
        )

    @staticmethod
    def _proxy_url(proxy: dict[str, str] | None) -> str | None:
        if not proxy:
            return None
        server = str(proxy.get("server") or "").strip()
        if not server:
            return None
        username = str(proxy.get("username") or "")
        password = str(proxy.get("password") or "")
        if not username:
            return server
        parsed = urlsplit(server)
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        return urlunsplit(
            (
                parsed.scheme,
                f"{auth}@{host}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "TelegramNotifier":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict:
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                data=data,
                files=files,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(
                f"Telegram 网络连接失败：{type(exc).__name__}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram 返回格式错误（HTTP {response.status_code}）"
            ) from exc
        if (
            response.status_code >= 400
            or not isinstance(payload, dict)
            or not payload.get("ok")
        ):
            description = (
                str(payload.get("description") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            raise TelegramError(
                description
                or f"Telegram 推送失败（HTTP {response.status_code}）"
            )
        return payload

    def send_test(self) -> dict:
        return self._request(
            "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": "CTExcel 客户端 Telegram 推送测试成功",
            },
        )

    def send_payment_qr(
        self,
        image: bytes,
        *,
        caption: str,
    ) -> dict:
        if not image:
            raise TelegramError("付款二维码截图为空")
        return self._request(
            "sendPhoto",
            data={
                "chat_id": self.chat_id,
                "caption": caption[:1024],
            },
            files={
                "photo": (
                    "ctexcel-wechat-payment.png",
                    image,
                    "image/png",
                )
            },
        )
