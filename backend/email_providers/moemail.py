"""MoEmail provider implementation.

Wraps the lower-level MoEmailClient into the EmailProvider abstraction.
"""
from __future__ import annotations

import re

from ._moemail_client import MoEmailClient, generate_email_name
from .base import EmailProvider, GeneratedEmail, InboxMessage


VERIFICATION_CODE_RE = re.compile(r"\b\d{6}\b")


# Default expiry time for newly generated MoEmail accounts: 0 = 永久.
#
# beilunyang/moemail (the upstream project this code targets) only accepts
# one of these four values in `expiryTime` (see app/types/email.ts):
#     0           (永久, expiresAt = "9999-01-01")
#     3600000     (1 小时)
#     86400000    (1 天)
#     259200000   (3 天)
# Any other value (including "10 years" 315360000000) is rejected with
# 400 `{"error":"无效的过期时间"}`. The README on the upstream repo
# mentions 7 days but the actual code is 3 days — we go with the code.
#
# The 0 / 永久 sentinel maps server-side to a year-9999 timestamp, which
# is effectively "never expires" for any practical purpose. This is what
# giffgaff customers need: their email inbox must stay reachable for
# the lifetime of the SIM.
#
# Operators can override per provider via the `expiry_time_ms` field on
# EmailProviderCreate/Update (1 hour / 1 day / 3 day are the only other
# options the upstream server accepts). The override is persisted in
# `config_json.expiry_time_ms`.
DEFAULT_EXPIRY_TIME_MS = 0


class MoEmailProvider(EmailProvider):
    provider_type = "moemail"

    def __init__(self, url: str, api_key: str, *, domains: list[str] | None = None,
                 default_domain: str | None = None,
                 expiry_time_ms: int | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self._client = MoEmailClient(url, api_key)
        self._domains = list(domains or [])
        self._default_domain = default_domain
        # Default to 永久 (0). 0 is intentionally allowed as a valid
        # value here — beilunyang/moemail treats it as the "永久" sentinel
        # and stores expiresAt as "9999-01-01".
        self._expiry_time_ms = DEFAULT_EXPIRY_TIME_MS if expiry_time_ms is None else int(expiry_time_ms)

    def _pick_domain(self, requested: str | None = None) -> str | None:
        if requested:
            req = requested.strip()
            if not req:
                return self._default_domain
            if not self._domains or req in self._domains:
                return req
            # Requested domain not in the provider's allow-list — fall back to provider default.
            return self._default_domain
        return self._default_domain

    def generate_email(self, *, domain: str | None = None) -> GeneratedEmail:
        chosen = self._pick_domain(domain)
        data = self._client.generate_email(
            name=generate_email_name(),
            expiry_time=self._expiry_time_ms,
            domain=chosen,
        )
        share_link = None
        try:
            result = self._client.create_share_link(data["id"])
            share_link = result.get("link")
        except Exception:
            share_link = None
        return GeneratedEmail(
            provider_account_id=str(data["id"]),
            address=data["email"],
            share_link=share_link,
        )

    def list_domains(self) -> list[str]:
        if not self._domains:
            return []
        if self._default_domain and self._default_domain not in self._domains:
            return [self._default_domain] + self._domains
        return list(self._domains)

    def default_domain(self) -> str | None:
        return self._default_domain

    def fetch_latest_messages(
        self, provider_account_id: str, *, after_message_id: str = ""
    ) -> list[InboxMessage]:
        data = self._client.get_email_messages(provider_account_id)
        msgs = []
        for m in data.get("messages", []):
            mid = str(m.get("id"))
            if after_message_id and mid <= after_message_id:
                continue
            try:
                body = self._client.get_message(provider_account_id, mid)
            except Exception:
                continue
            # beilunyang/moemail wraps the message body under a "message" key.
            # Also tolerate the legacy flat shape.
            inner = body.get("message") if isinstance(body, dict) else None
            if not isinstance(inner, dict):
                inner = body if isinstance(body, dict) else {}
            text = inner.get("text") or inner.get("content") or ""
            msgs.append(InboxMessage(
                id=mid,
                subject=m.get("subject", "") or inner.get("subject", ""),
                text=text,
                received_at=m.get("receivedAt") or m.get("createdAt") or "",
            ))
        return msgs

    def get_email_messages(self, provider_account_id: str) -> dict:
        """Return the provider's raw summary payload without fetching every body.

        Full bodies are loaded later through ``get_message``. Keeping the list
        operation summary-only matters for large permanent inboxes.
        """
        payload = self._client.get_email_messages(provider_account_id)
        return payload if isinstance(payload, dict) else {"messages": []}

    def get_message(self, provider_account_id: str, message_id: str) -> dict:
        """Return the inner message body, unwrapping beilunyang/moemail's
        `{"message": {...}}` envelope. Mirrors CloudMailProvider's
        signature so main.py can call `client.get_message(...)` on either
        provider and get a flat body dict back.

        Without this alias, MoEmailProvider raises AttributeError in
        main.py:get_customer_verification_code, leaving moemail customers
        unable to read their verification codes even though the same
        CloudMailProvider-shaped flow works for cloud-mail customers.
        """
        body = self._client.get_message(provider_account_id, message_id)
        if isinstance(body, dict) and isinstance(body.get("message"), dict):
            return body["message"]
        return body if isinstance(body, dict) else {}

    def extract_verification_code(self, message: InboxMessage) -> str | None:
        m = VERIFICATION_CODE_RE.search(message.text)
        return m.group(0) if m else None

    def ping(self) -> bool:
        try:
            self._client.get_config()
            return True
        except Exception:
            return False
