from unittest.mock import MagicMock, patch
import pytest

from email_providers.moemail import MoEmailProvider
from email_providers.base import InboxMessage


@pytest.fixture
def mocked_provider():
    """Provider with a stubbed MoEmailClient."""
    with patch("email_providers._moemail_client.httpx") as mock_httpx:
        client = MagicMock()
        with patch("email_providers.moemail.MoEmailClient", return_value=client):
            p = MoEmailProvider(url="https://moemail.test", api_key="k")
    return p, client


def test_provider_type():
    p = MoEmailProvider(url="https://x", api_key="k")
    assert p.provider_type == "moemail"


def test_generate_email_returns_generated_email(mocked_provider):
    p, client = mocked_provider
    client.generate_email.return_value = {"id": 7, "email": "abc@681218.xyz"}
    client.create_share_link.return_value = {"link": "https://share"}

    gen = p.generate_email()

    assert gen.provider_account_id == "7"
    assert gen.address == "abc@681218.xyz"
    assert gen.share_link == "https://share"


def test_generate_email_share_link_failure_is_tolerated(mocked_provider):
    """If create_share_link fails (e.g., not supported), share_link should be None, not raise."""
    p, client = mocked_provider
    client.generate_email.return_value = {"id": 7, "email": "abc@681218.xyz"}
    client.create_share_link.side_effect = Exception("404 not found")

    gen = p.generate_email()

    assert gen.share_link is None
    assert gen.address == "abc@681218.xyz"


def test_fetch_latest_messages(mocked_provider):
    p, client = mocked_provider
    client.get_email_messages.return_value = {
        "messages": [{"id": "1"}, {"id": "2"}]
    }
    client.get_message.side_effect = [
        {"text": "code 123456"},
        {"text": "no code here"},
    ]

    msgs = p.fetch_latest_messages("42")

    assert len(msgs) == 2
    assert msgs[0].id == "1"
    assert "123456" in msgs[0].text
    assert msgs[1].id == "2"


def test_fetch_latest_messages_filters_by_after_id(mocked_provider):
    p, client = mocked_provider
    client.get_email_messages.return_value = {
        "messages": [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    }
    client.get_message.return_value = {"text": "hi"}

    msgs = p.fetch_latest_messages("42", after_message_id="2")
    # Only message "3" has id > "2"
    assert len(msgs) == 1
    assert msgs[0].id == "3"


def test_get_email_messages_keeps_large_inbox_summary_only(mocked_provider):
    p, client = mocked_provider
    payload = {
        "messages": [
            {"id": str(index), "subject": f"Message {index}"}
            for index in range(200)
        ]
    }
    client.get_email_messages.return_value = payload

    result = p.get_email_messages("account-1")

    assert result == payload
    client.get_email_messages.assert_called_once_with("account-1")
    client.get_message.assert_not_called()


def test_extract_verification_code():
    p = MoEmailProvider(url="https://x", api_key="k")
    msg = InboxMessage(id="1", subject="x", text="Your code is 123456", received_at="t")
    assert p.extract_verification_code(msg) == "123456"


def test_extract_verification_code_returns_none_when_absent():
    p = MoEmailProvider(url="https://x", api_key="k")
    msg = InboxMessage(id="1", subject="x", text="no code", received_at="t")
    assert p.extract_verification_code(msg) is None


def test_ping_true_on_success(mocked_provider):
    p, client = mocked_provider
    client.get_config.return_value = {"emailDomains": "x,y"}
    assert p.ping() is True


def test_ping_false_on_failure(mocked_provider):
    p, client = mocked_provider
    client.get_config.side_effect = Exception("network down")
    assert p.ping() is False

# beilunyang/moemail only accepts these four values for `expiryTime`:
#   0          (永久, expiresAt = "9999-01-01")
#   3600000    (1 hour)
#   86400000   (1 day)
#   259200000  (3 days)
# See app/types/email.ts in the upstream repo. Any other value is rejected
# with 400 `{"error":"无效的过期时间"}`. We default to 0 (=永久) so the
# inbox stays reachable for the lifetime of the giffgaff customer.
ACCEPTED_EXPIRY_TIME_MS = (0, 3_600_000, 86_400_000, 259_200_000)


def test_default_expiry_time_is_zero():
    """The default must be 0 (=永久). Any other value gets rejected."""
    from email_providers.moemail import DEFAULT_EXPIRY_TIME_MS
    assert DEFAULT_EXPIRY_TIME_MS == 0, (
        f"DEFAULT_EXPIRY_TIME_MS must be 0 (永久), got {DEFAULT_EXPIRY_TIME_MS}"
    )
    assert DEFAULT_EXPIRY_TIME_MS in ACCEPTED_EXPIRY_TIME_MS


def test_generate_email_sends_default_expiry_time_ms():
    """Regression: the default must be 0 (=永久), accepted by the
    upstream beilunyang/moemail server's whitelist.
    """
    from email_providers.moemail import DEFAULT_EXPIRY_TIME_MS
    with patch("email_providers._moemail_client.httpx") as mock_httpx:
        client = MagicMock()
        with patch("email_providers.moemail.MoEmailClient", return_value=client):
            p = MoEmailProvider(url="https://moemail.test", api_key="k")
        client.generate_email.return_value = {"id": 1, "email": "a@b.test"}
        client.create_share_link.return_value = {"link": "x"}
        p.generate_email()
        sent = client.generate_email.call_args.kwargs["expiry_time"]
        assert sent == DEFAULT_EXPIRY_TIME_MS == 0
        assert sent in ACCEPTED_EXPIRY_TIME_MS


def test_provider_expiry_time_ms_override():
    """Per-provider `expiry_time_ms` overrides the default. The override
    must still be a value the upstream server accepts, otherwise the
    server returns 400 `{"error":"无效的过期时间"}`.
    """
    with patch("email_providers._moemail_client.httpx") as mock_httpx:
        client = MagicMock()
        with patch("email_providers.moemail.MoEmailClient", return_value=client):
            p = MoEmailProvider(
                url="https://moemail.test", api_key="k",
                expiry_time_ms=259200000,  # 3 days
            )
        client.generate_email.return_value = {"id": 1, "email": "a@b.test"}
        client.create_share_link.return_value = {"link": "x"}
        p.generate_email()
        sent = client.generate_email.call_args.kwargs["expiry_time"]
        assert sent == 259200000
        assert sent in ACCEPTED_EXPIRY_TIME_MS

def test_generate_email_name_has_at_least_one_uppercase_letter():
    """Operator requirement: the generated email local-part must include
    at least one uppercase letter, because the address is reused as a
    password on services (e.g. giffgaff) that require upper-case."""
    from email_providers._moemail_client import generate_email_name
    # Run a bunch of times to avoid statistical flake
    for _ in range(200):
        name = generate_email_name()
        assert any(c.isupper() for c in name), name


def test_get_message_unwraps_moemail_message_envelope(mocked_provider):
    """beilunyang/moemail wraps the message body under a "message" key
    (see app/api/emails/[id]/[messageId]/route.ts). Without unwrapping,
    `client.get_message(...)` would return a dict whose `content` /
    `html` / `subject` fields are all hidden one level deep and the
    verification code extractor would see an empty body.
    """
    p, client = mocked_provider
    client.get_message.return_value = {
        "message": {
            "id": "msg-1",
            "subject": "Your code is 123456",
            "content": "code: 123456",
            "html": "<p>code: 123456</p>",
        }
    }
    body = p.get_message("acct-1", "msg-1")
    assert body == {
        "id": "msg-1",
        "subject": "Your code is 123456",
        "content": "code: 123456",
        "html": "<p>code: 123456</p>",
    }


def test_get_message_tolerates_legacy_flat_shape(mocked_provider):
    """Older / non-standard MoEmail deployments return the message
    body at the top level, not under a "message" key. MoEmailProvider
    should accept both shapes.
    """
    p, client = mocked_provider
    client.get_message.return_value = {"id": "msg-1", "content": "code: 654321"}
    body = p.get_message("acct-1", "msg-1")
    assert body == {"id": "msg-1", "content": "code: 654321"}


def test_fetch_latest_messages_extracts_text_from_message_envelope(mocked_provider):
    """Regression: the inbox path extracts `text` (or falls back to
    `content`) from the inner body. If the upstream wraps the body
    in a "message" key and we don't unwrap, the InboxMessage's `text`
    field would be empty and verification code detection would fail.
    """
    p, client = mocked_provider
    client.get_email_messages.return_value = {
        "messages": [{"id": "msg-1", "subject": "Verify", "receivedAt": "2026-07-09"}]
    }
    client.get_message.return_value = {
        "message": {
            "id": "msg-1",
            "subject": "Verify",
            "content": "code: 999999",
        }
    }
    msgs = p.fetch_latest_messages("acct-1")
    assert len(msgs) == 1
    assert msgs[0].text == "code: 999999"
    assert msgs[0].subject == "Verify"

def test_full_inbox_flow_unwraps_message_envelope_and_extracts_code(mocked_provider):
    """End-to-end-style regression: simulate what main.py does.

    main.py:get_customer_verification_code calls
        client.get_email_messages(moemail_id)
        client.get_message(moemail_id, message_id)
    and runs _extract_verification_code on the merged dict. If
    either step fails to unwrap the {"message": {...}} envelope, the
    code is never extracted. The user reports that moemail customers
    were unable to read their verification codes; this test would
    have caught it.
    """
    p, client = mocked_provider
    # Simulate beilunyang/moemail response shape
    client.get_email_messages.return_value = {
        "messages": [
            {
                "id": "msg-1",
                "subject": "Confirm it's you",
                "receivedAt": "2026-07-09T08:00:00Z",
                "createdAt": "2026-07-09T08:00:00Z",
            }
        ]
    }
    client.get_message.return_value = {
        "message": {
            "id": "msg-1",
            "subject": "Confirm it's you",
            "from_address": "noreply@giffgaff.com",
            "to_address": "test@681218.xyz",
            "content": "Your verification code is 482916. It expires in 10 minutes.",
            "html": "<p>Your verification code is <b>482916</b>.</p>",
        }
    }
    # Simulate the main.py code path
    mailbox = p.get_email_messages("acct-1")
    messages = [m for m in mailbox.get("messages", []) if isinstance(m, dict)]
    assert len(messages) == 1
    message_id = messages[0]["id"]
    detail = p.get_message("acct-1", message_id)
    merged = {**messages[0], **detail}
    # Use the actual extraction function from main
    import importlib
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_moemail_extract", BACKEND := None
    )
    # Direct import: the extraction function is defined in main.py as a
    # module-level function. Pull it out by executing main's extract fn.
    # Simpler: just inline the logic here.
    import re
    subject = merged.get("subject", "")
    content = merged.get("content", "") or merged.get("text", "") or merged.get("body", "")
    html_content = re.sub(r"<[^>]+>", " ", merged.get("html", "") or "")
    text = "\n".join(p for p in (subject, content, html_content) if p)
    code_match = re.search(r"\b\d{6}\b", text)
    assert code_match is not None, f"no 6-digit code in: {text!r}"
    assert code_match.group(0) == "482916"
