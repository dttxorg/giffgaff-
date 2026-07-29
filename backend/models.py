from pydantic import BaseModel, Field
from datetime import date
from typing import Any, Literal, Optional


PhoneStatus = Literal["激活", "封号", "投诉", "退款", "丢失", "作废"]
ActivationStatus = Literal["未开始", "已分配激活码", "激活中", "等待人工支付", "等待转 eSIM", "已完成", "失败"]
SimCodeStatus = Literal["未分配", "已分配", "激活中", "已使用", "失败", "作废"]
ProductType = Literal["giffgaff", "ctexcel"]


class CustomerCreate(BaseModel):
    product_type: ProductType = "giffgaff"
    phone_number: Optional[str] = None
    email: str = ""
    shipping_address: Optional[str] = None
    phone_status: PhoneStatus = "激活"
    courier_company: Optional[str] = None
    tracking_number: Optional[str] = None
    courier_order_code: Optional[str] = None
    courier_print_data: Optional[str] = None
    activation_date: date
    use_sim_code: bool = True
    email_provider_id: Optional[int] = None  # None = pool round-robin
    email_provider_domain: Optional[str] = None  # explicit domain; None = provider default


class CustomerUpdate(BaseModel):
    phone_number: Optional[str] = None
    email: Optional[str] = None
    shipping_address: Optional[str] = None
    phone_status: Optional[PhoneStatus] = None
    courier_company: Optional[str] = None
    tracking_number: Optional[str] = None
    courier_order_code: Optional[str] = None
    courier_print_data: Optional[str] = None
    activation_date: Optional[date] = None
    activation_status: Optional[ActivationStatus] = None
    activation_error: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    ctexcel_order_number: Optional[str] = None
    ctexcel_transaction_amount: Optional[str] = None
    ctexcel_referral_code: Optional[str] = None
    ctexcel_referral_link: Optional[str] = None


class CustomerOut(BaseModel):
    id: int
    product_type: ProductType = "giffgaff"
    phone_number: Optional[str]
    email: str
    shipping_address: Optional[str]
    phone_status: PhoneStatus
    courier_company: Optional[str]
    tracking_number: Optional[str]
    courier_order_code: Optional[str]
    activation_date: date
    moemail_id: Optional[str]
    moemail_address: Optional[str]
    email_provider_id: Optional[int] = None
    email_account_id: Optional[str] = None
    share_link: Optional[str]
    is_moemail_auto: bool
    sim_code_id: Optional[int] = None
    sim_activation_code: Optional[str] = None
    public_token: Optional[str] = None
    public_version: int = 1
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    ctexcel_order_number: Optional[str] = None
    ctexcel_transaction_amount: Optional[str] = None
    ctexcel_referral_code: Optional[str] = None
    ctexcel_referral_link: Optional[str] = None
    ctexcel_last_checked_at: Optional[str] = None
    payment_changed_at: Optional[str] = None
    payment_updated_at: Optional[str] = None
    payment_last_checked_at: Optional[str] = None
    esim_raw_code: Optional[str] = None
    activation_status: ActivationStatus = "未开始"
    activation_error: Optional[str] = None
    activated_at: Optional[str] = None
    created_at: str


class CustomerDetail(BaseModel):
    id: int
    product_type: ProductType = "giffgaff"
    phone_number: Optional[str]
    email: str
    shipping_address: Optional[str]
    phone_status: PhoneStatus
    courier_company: Optional[str]
    tracking_number: Optional[str]
    courier_order_code: Optional[str]
    activation_date: date
    created_at: str
    moemail_id: Optional[str]
    moemail_address: Optional[str]
    email_provider_id: Optional[int] = None
    email_account_id: Optional[str] = None
    share_link: Optional[str]
    is_moemail_auto: bool
    sim_code_id: Optional[int] = None
    sim_activation_code: Optional[str] = None
    public_token: Optional[str] = None
    public_version: int = 1
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    ctexcel_order_number: Optional[str] = None
    ctexcel_transaction_amount: Optional[str] = None
    ctexcel_referral_code: Optional[str] = None
    ctexcel_referral_link: Optional[str] = None
    ctexcel_last_checked_at: Optional[str] = None
    payment_changed_at: Optional[str] = None
    payment_updated_at: Optional[str] = None
    payment_last_checked_at: Optional[str] = None
    initial_password: Optional[str] = None
    esim_raw_code: Optional[str] = None
    activation_status: ActivationStatus = "未开始"
    activation_error: Optional[str] = None
    activated_at: Optional[str] = None


class SystemSettings(BaseModel):
    app_mode: Optional[ProductType] = None
    giffgaff_download_url: Optional[str] = None
    activation_tutorial_url: Optional[str] = None
    activation_page_markdown: Optional[str] = None
    activation_page_version: Optional[int] = None
    public_page_markdown: Optional[str] = None
    public_worker_domain: Optional[str] = None  # 留空则 QR 编码用当前域名
    custom_public_vars: Optional[str] = None  # JSON: {"var_name": "value"}，可在 public_page_markdown 里用 {var_name} 引用


class AuthLoginRequest(BaseModel):
    password: str


class CTExcelClientCustomerCreate(BaseModel):
    shipping_address: Optional[str] = None


class MoEmailCreateRequest(BaseModel):
    domain: Optional[str] = None


class SimCodeImport(BaseModel):
    codes: Optional[list[str]] = None
    text: Optional[str] = None


class SimCodeUpdate(BaseModel):
    status: SimCodeStatus


class SimCodeOut(BaseModel):
    id: int
    code: str
    status: SimCodeStatus
    customer_id: Optional[int] = None
    notes: Optional[str] = None
    created_at: str
    updated_at: str


class ActivationStatusUpdate(BaseModel):
    status: ActivationStatus
    error: Optional[str] = None
    step: Optional[str] = None
    message: Optional[str] = None


class VerificationCodeOut(BaseModel):
    found: bool
    code: Optional[str] = None
    email: str = ""
    message_id: Optional[str] = None
    subject: Optional[str] = None
    from_address: Optional[str] = None
    received_at: Optional[str] = None
    checked_count: int = 0
    detail: str = ""


class PaymentInfoEmailOut(BaseModel):
    found: bool = False
    updated_found: bool = False
    changed_found: bool = False
    updated_count: int = 0
    changed_count: int = 0
    email: str = ""
    checked_count: int = 0
    latest_updated_message_id: Optional[str] = None
    latest_updated_subject: Optional[str] = None
    latest_updated_received_at: Optional[str] = None
    latest_changed_message_id: Optional[str] = None
    latest_changed_subject: Optional[str] = None
    latest_changed_received_at: Optional[str] = None
    detail: str = ""


class CTExcelOrderInfoOut(BaseModel):
    found: bool = False
    phone_number: Optional[str] = None
    order_number: Optional[str] = None
    transaction_amount: Optional[str] = None
    referral_code: Optional[str] = None
    referral_link: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None
    from_address: Optional[str] = None
    received_at: Optional[str] = None
    checked_count: int = 0
    detail: str = ""


class InboxMessageSummaryOut(BaseModel):
    id: str
    subject: str = ""
    from_address: str = ""
    sent_at: str = ""
    received_at: str = ""


class InboxMessageListOut(BaseModel):
    email: str = ""
    count: int = 0
    messages: list[InboxMessageSummaryOut] = Field(default_factory=list)


class InboxMessageDetailOut(BaseModel):
    id: str
    subject: str = ""
    from_address: str = ""
    to_address: str = ""
    sent_at: str = ""
    received_at: str = ""
    body: str = ""
    html_body: str = ""


class DomainInfo(BaseModel):
    domains: list[str]


class LabelConfig(BaseModel):
    giffgaff_download_url: str
    activation_tutorial_url: str = "https://gg.681218.xyz/activation.html"
    default_template_id: Optional[str] = None
    templates: list[dict[str, Any]]


class EsimCodeUpdate(BaseModel):
    code: str = ""


class EmailProviderCreate(BaseModel):
    name: str
    provider_type: str  # 'moemail' | 'cloudmail'
    config: dict
    domains: list[str] = []
    default_domain: Optional[str] = None
    disabled: bool = False
    expiry_time_ms: Optional[int] = None  # moemail only; 0 / None = default 7 days


class EmailProviderOut(BaseModel):
    id: int
    name: str
    provider_type: str
    config: dict
    domains: list[str] = []
    default_domain: Optional[str] = None
    disabled: bool = False
    expiry_time_ms: Optional[int] = None
    last_used_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    last_jwt_acquired_at: Optional[str] = None
    created_at: str
    updated_at: str


class EmailProviderUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict] = None
    domains: Optional[list[str]] = None
    default_domain: Optional[str] = None
    disabled: Optional[bool] = None
    expiry_time_ms: Optional[int] = None


class ResetCustomerRequest(BaseModel):
    detach_sim_code: bool = True
    detach_email: bool = True
    reset_activation: bool = True


class EmailProviderDomainPick(BaseModel):
    domain: Optional[str] = None  # None -> use provider's default_domain
