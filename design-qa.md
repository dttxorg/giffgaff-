# Dense customer ledger — design QA

## Evidence

- Source visual truth: `/Users/zhuli/.codex/generated_images/019f50ad-93c7-7803-8ffb-0d1af4bc1497/exec-8359100e-be55-4b9d-8cf0-dc8e5df546dd.png`
- Desktop default list: `/tmp/giffgaff-dense-ledger-final/01-desktop-list.png`
- Desktop on-demand detail: `/tmp/giffgaff-dense-ledger-final/02-desktop-detail.png`
- Mobile list: `/tmp/giffgaff-dense-ledger-final/03-mobile-list.png`
- Mobile full-screen detail: `/tmp/giffgaff-dense-ledger-final/04-mobile-detail.png`
- Desktop paginated ledger: `/tmp/giffgaff-registration-pagination/01-desktop-pagination.png`
- Desktop registration-first detail: `/tmp/giffgaff-registration-pagination/02-desktop-registration-detail.png`
- Mobile paginated ledger: `/tmp/giffgaff-registration-pagination/03-mobile-pagination.png`
- Mobile registration-first detail: `/tmp/giffgaff-registration-pagination/04-mobile-registration-detail.png`
- Desktop ledger with shipping address: `/tmp/giffgaff-print-address/01-desktop-address.png`
- Print panel above an open customer detail: `/tmp/giffgaff-print-address/02-print-over-detail.png`
- Mobile customer card with wrapped shipping address: `/tmp/giffgaff-print-address/03-mobile-address.png`
- Desktop full-inbox list and message reader: `/tmp/giffgaff-full-inbox/01-desktop.png`
- Mobile full-inbox list and message reader: `/tmp/giffgaff-full-inbox/02-mobile.png`
- Desktop HTML email reader with sent/received timestamps: `/tmp/giffgaff-inbox-html-sync/01-html-reader.png`
- Full-view comparison: `/tmp/giffgaff-dense-ledger-final/desktop-list-comparison.png`
- Focused table comparison: `/tmp/giffgaff-dense-ledger-final/desktop-table-comparison.png`
- Desktop viewport and state: `1440 × 1024`, unfiltered default customer ledger.
- Mobile viewport and state: `390 × 844`, default ledger and first-customer detail.

## Findings

No actionable P0, P1, or P2 issue remains.

- [P3] The implementation keeps the product's existing text navigation and header instead of adopting the generated target's icon-heavy top navigation.
  - Evidence: `/tmp/giffgaff-dense-ledger-final/desktop-list-comparison.png`.
  - Impact: minor visual drift; existing navigation remains familiar and avoids introducing an external icon dependency.
- [P3] The implementation shows 19 visible customer rows instead of the target's 15 and omits the target's decorative date-range control.
  - Evidence: browser measurement reported 19 visible rows at `1440 × 1024`.
  - Impact: information density is higher; search plus four working status filters cover the current operational need.

## Required fidelity surfaces

- Fonts and typography: compact system sans and monospace data values match the source's operations-console direction. Desktop body/data text remains readable at 12–14px; mobile labels and values wrap without clipping.
- Spacing and layout rhythm: the customer area now uses the full available width. Rows are 44px high with aligned column headers and lightweight separators. The former permanent detail column and empty right-side state are gone.
- Colors and visual tokens: existing purple navigation/header, white ledger surface, subtle dividers, and semantic green/orange/red/purple badges closely match the source palette and status hierarchy.
- Image and asset fidelity: this admin screen requires no photography or illustration. No new placeholder image, handcrafted SVG, CSS drawing, gradient, or emoji asset was added; the old inline search SVG was removed.
- Copy and content: every default row exposes 手机号、邮箱、收货地址、号码状态、激活状态、SIM 激活码、解绑查询、快递公司、快递单号、开通日期和详情操作. Existing customer, QR, email, activation, payment, shipping and tool copy remains available in the opened detail.
- Responsiveness: desktop has no document-level horizontal overflow. Mobile converts each ledger row into a two-column record card, renders all four filters in a visible 2 × 2 grid, and opens detail as a full `390 × 844` screen from scroll position 0.
- Accessibility and states: rows remain keyboard-openable; phone numbers and detail actions are semantic buttons; filters have accessible names; mobile has a visible 返回客户列表 action; final browser console checks returned zero errors or warnings.

## Primary interactions tested

- Search field renders against the full-width ledger.
- 号码状态筛选 set to 封号 returns four matching rows; reset restores 28 rows.
- Clicking the phone number opens a temporary `1200 × 900` desktop detail modal.
- Closing the detail restores the full-width customer ledger.
- Mobile detail opens full-screen, starts at scroll position 0, and exposes the labeled return action.
- Existing independent 打印标签 and 打印快递单 actions remain in the detail header; their print panel is layered above the still-open customer detail.
- The new 完整邮箱 section reads every available message summary on demand, opens the newest body automatically, switches bodies from the left-hand list, and exposes a copy-body action without using the public Worker page.
- HTML email content renders in a sandboxed, CSP-restricted iframe in the dedicated right-hand reading column; the operator can switch back to extracted plain text, while sent and received timestamps remain visible above the body.
- Numeric Unix timestamps from email providers are normalized server-side and displayed as both Asia/Shanghai and Europe/London local time in the reader.
- Payment-card query results are committed before the endpoint returns success, then the customer ledger and active detail record are reloaded so the 首页 badge changes immediately.
- Customer pagination defaults to 20 rows: page 1 shows 1–20 of 28, page 2 shows the remaining 8, and changing the page size to 50 collapses the result to one page.
- The detail first screen exposes registration email, SIM activation code and email verification code without changing sections. The obsolete initial-password control is removed because the registration email is now used as the password.
- The random identity and UK address block opens only on demand and resets to its collapsed 44px state whenever detail opens.
- At `390 × 844`, the registration fields stack into one readable column, the verification action stays beside its label, the verification value remains full width, and the detail starts at scroll position 0.
- JavaScript syntax, backend tests, Worker tests and diff checks pass.

## Comparison history

### Iteration 1

- [P1] The previous redesign permanently allocated the right side to customer detail, leaving a large empty region before selection and reducing the default list to about five visible customers.
  - Fix: replaced the master/detail split with a full-width ledger and moved detail into an on-demand modal.
  - Post-fix evidence: `/tmp/giffgaff-dense-ledger-final/01-desktop-list.png` shows 19 visible customer rows and no reserved detail region.
- [P1] Customer cards hid core comparison fields and made cross-customer scanning slow.
  - Fix: restored a true table with ten visible operational columns and compact 44px rows.
  - Post-fix evidence: `/tmp/giffgaff-dense-ledger-final/desktop-table-comparison.png`.

### Iteration 2

- [P2] The first implementation capture truncated common email addresses.
  - Fix: redistributed column widths so a normal address such as `customer01@example-mail.uk` fits without clipping at the target desktop viewport.
  - Post-fix browser measurement: email cell `clientWidth` and `scrollWidth` both equal 251px.
- [P2] Mobile filters initially overflowed as one horizontal strip.
  - Fix: changed mobile filters to a visible 2 × 2 grid with a full-width reset action.
  - Post-fix evidence: `/tmp/giffgaff-dense-ledger-final/03-mobile-list.png`.

### Final pass

- Re-captured desktop and mobile after the fixes.
- Re-ran full-view and focused source comparisons.
- No actionable P0/P1/P2 difference remains.

## Implementation checklist

- [x] Full-width, high-density desktop customer ledger.
- [x] Eleven default data columns, including the shipping address.
- [x] Working phone, activation, payment and shipping filters.
- [x] Customer detail opens only after clicking a number, row or 详情.
- [x] Desktop temporary modal and mobile full-screen detail.
- [x] Existing customer-management functions preserved.
- [x] Label and courier printing remain separate.
- [x] Worker `API_BASE` and public QR implementation unchanged.
- [x] Client-side pagination with 20/50/100 page-size choices.
- [x] Registration email, SIM code and mailbox verification on the default detail screen.
- [x] Print panels open above customer detail without forcing the detail to close.
- [x] Direct full-inbox list and plain-text body reader for provider-backed customer email accounts.
- [x] Sandboxed HTML preview, plain-text fallback, sent/received timestamps and immediate payment-status synchronization.
- [x] Random identity and UK address collapsed by default.
- [x] Persistent giffgaff / CTExcel mode switch with product-specific customer lists.
- [x] CTExcel desktop ledger replaces SIM/payment columns with order number and mailbox-sync state.
- [x] CTExcel detail hides SIM, eSIM, random UK identity, payment check and voicemail tools.
- [x] CTExcel order fields remain visible and copyable without opening the full mailbox.
- [x] CTExcel public QR page has its own responsive visual system and contains no giffgaff voicemail flow.

## CTExcel mode verification

- Desktop `1440 × 1024`: the CTExcel ledger displayed 10 aligned columns and only CTExcel customers; the persistent switch restored the giffgaff ledger, SIM tab and giffgaff-only filters.
- Mobile `390 × 844`: header mode controls remained horizontally usable, customer rows rendered as two-column cards, and order/sync fields stayed readable.
- CTExcel detail: tabs were limited to 账号与订单、发货、完整邮箱、工具. Order number, amount and referral code were visible in the first section.
- CTExcel public page: mobile viewport rendered the phone, email, order number and referral code as separate copy rows; no second tutorial jump, eSIM, payment or voicemail section appeared.
- Persistence: switching to CTExcel, reloading `/index.html`, and reading the page again retained CTExcel as the active mode.
- Browser console: zero error-level entries after list, detail, public page, mobile and mode-switch checks.

final result: passed

## CTExcel Windows client v2.3.8 verification

- Desktop `1440 × 1000`: the proxy-pool mode exposes a full-width multiline
  paste target, one-click clipboard import, SOCKS5 protocol selection and the
  5–8-use rotation controls without clipping or horizontal overflow.
- The lower settings view keeps Telegram Bot Token, Chat ID and test delivery in
  one compact card above the fixed registration data; all labels and controls
  remain readable against the light surface.
- Authenticated SOCKS5 nodes are exposed to Chrome/Edge through a loopback-only
  unauthenticated bridge, while the bridge authenticates to the upstream node.
- Continuous application supports 1–10 isolated browser workers, idempotent
  customer allocation and a shared thread-safe proxy-pool rotation counter.
- Each payment page sends the detected WeChat QR image (or a viewport fallback)
  to the configured Telegram bot without blocking payment completion.
- Evidence: `/tmp/ctexcel-v238-proxy-pool.png` and
  `/tmp/ctexcel-v238-settings-bottom.png`.
- Client tests, 242 backend tests, 15 Worker tests, Python compilation and diff
  checks pass.

final result: passed
