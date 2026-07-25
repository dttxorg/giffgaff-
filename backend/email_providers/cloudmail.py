"""cloud-mail provider implementation (github.com/maillab/cloud-mail).

Uses JWT auth: on each operation, ensures a fresh JWT (re-login if missing or >25d old).
"""
from __future__ import annotations

import random
import re
import string
from datetime import datetime, timezone, timedelta

import httpx

from .base import EmailProvider, GeneratedEmail, InboxMessage

VERIFICATION_CODE_RE = re.compile(r"\b\d{6}\b")
JWT_REFRESH_DAYS = 25


def generate_random_prefix() -> str:
    """Build a 10-char local-part and ensure at least one ASCII letter
    is uppercased so the resulting address can be used directly as a
    password on services (e.g. giffgaff) whose password policy
    requires an upper-case character.

    Mirrors the same guarantee in MoEmailClient.generate_email_name().
    """
    chars = string.ascii_lowercase + string.digits
    prefix = "".join(random.choices(chars, k=10))
    letter_positions = [i for i, ch in enumerate(prefix) if ch.isalpha()]
    pos = random.choice(letter_positions)
    return prefix[:pos] + prefix[pos].upper() + prefix[pos + 1:]


class CloudMailProvider(EmailProvider):
    provider_type = "cloudmail"

    def __init__(
        self,
        *,
        url: str,
        email: str,
        password: str,
        domain: str,
        jwt_token: str | None = None,
        jwt_acquired_at: str | None = None,
    ):
        self.base_url = url.rstrip("/")
        self._email = email
        self._password = password
        self._domain = domain
        self._jwt = jwt_token
        self._jwt_at = jwt_acquired_at  # ISO string

    # ---------- Auth ----------
    def _jwt_is_fresh(self) -> bool:
        if not self._jwt or not self._jwt_at:
            return False
        try:
            acquired = datetime.fromisoformat(self._jwt_at)
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        return now - acquired < timedelta(days=JWT_REFRESH_DAYS)

    def _ensure_jwt(self) -> None:
        if self._jwt_is_fresh():
            return
        r = httpx.post(
            f"{self.base_url}/api/login",
            json={"email": self._email, "password": self._password},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        token = (body.get("data") or {}).get("token") or body.get("token")
        if not token:
            raise RuntimeError(f"cloud-mail /login: missing token in response: {body!r}")
        self._jwt = token
        self._jwt_at = datetime.now(timezone.utc).isoformat()

    def _headers(self) -> dict:
        self._ensure_jwt()
        return {"Authorization": self._jwt}

    # Public introspection for auth.py to persist JWT to DB
    @property
    def jwt(self) -> str | None:
        return self._jwt

    @property
    def jwt_acquired_at(self) -> str | None:
        return self._jwt_at

    # ---------- Provider methods ----------
    def generate_email(self) -> GeneratedEmail:
        prefix = generate_random_prefix()
        email = f"{prefix}@{self._domain}"
        r = httpx.post(
            f"{self.base_url}/api/account/add",
            json={"email": email},
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data", {})
        return GeneratedEmail(
            provider_account_id=str(data["accountId"]),
            address=data.get("email", email),
            share_link=None,  # cloud-mail has no share_link concept
        )

    def fetch_latest_messages(
        self, provider_account_id: str, *, after_message_id: str = ""
    ) -> list[InboxMessage]:
        params = {"accountId": provider_account_id, "emailId": after_message_id or 0}
        r = httpx.get(
            f"{self.base_url}/api/email/latest",
            params=params,
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        items = (r.json() or {}).get("data", [])
        return [
            InboxMessage(
                id=str(m["emailId"]),
                subject=m.get("subject", ""),
                text=m.get("text", ""),
                received_at=m.get("createTime", ""),
            )
            for m in items
        ]

    def get_email_messages(self, provider_account_id: str) -> dict:
        """Return MoEmail-shaped JSON for main.py endpoint interop.

        cloud-mail's /email/latest uses `emailId` as the per-message id and
        doesn't separate summary from detail (the body is in the same payload).
        Returns a MoEmail-compatible summary list — main.py's message-detail
        paths will then call get_message() for the body.
        """
        messages = self.fetch_latest_messages(provider_account_id)
        return {
            "messages": [
                {
                    "id": int(m.id) if m.id.isdigit() else m.id,
                    "subject": m.subject,
                    "receivedAt": m.received_at,
                    "text": m.text,
                }
                for m in messages
            ]
        }

    def get_message(self, provider_account_id: str, message_id: str) -> dict:
        """cloud-mail has no per-message GET; look up by id in the already-fetched list."""
        results = self.fetch_latest_messages(provider_account_id, after_message_id="")
        for m in results:
            if m.id == str(message_id):
                return {"text": m.text, "subject": m.subject}
        return {}

    def extract_verification_code(self, message: InboxMessage) -> str | None:
        m = VERIFICATION_CODE_RE.search(message.text)
        return m.group(0) if m else None

    def ping(self) -> bool:
        try:
            self._ensure_jwt()
            r = httpx.get(
                f"{self.base_url}/api/my/loginUserInfo",
                headers=self._headers(),
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False
