# Prabhu Ventures — LoanTrack

A multi-tenant **SaaS lending management platform** built for asset-backed
(gold / silver / pledged-item) loan businesses. Tenants sign up with mobile
OTP, run their entire lending book from a clean dashboard, and pay via
subscription. The platform is operated by **Prabhu Ventures** with a single
admin role overseeing all tenants.

Live at **<https://prabhuventures.in>**.

---

## Table of Contents
1. [What this is](#1--what-this-is)
2. [Architecture at a glance](#2--architecture-at-a-glance)
3. [Tech stack](#3--tech-stack)
4. [Roles & access model](#4--roles--access-model)
5. [Feature set (current)](#5--feature-set-current)
6. [Data model](#6--data-model)
7. [HTTP API surface](#7--http-api-surface)
8. [Frontend architecture](#8--frontend-architecture)
9. [Local development](#9--local-development)
10. [Deploying](#10--deploying)
11. [Environment variables](#11--environment-variables)
12. [Provider abstractions](#12--provider-abstractions)
13. [Repository layout](#13--repository-layout)
14. [Project history (chronological)](#14--project-history-chronological)
15. [Phasing & roadmap](#15--phasing--roadmap)
16. [Contributing notes for future Claude/dev sessions](#16--contributing-notes-for-future-claudedev-sessions)

---

## 1 · What this is

Prabhu Ventures runs a family-rooted lending business. LoanTrack was first
built as the internal system to manage that business and has since been
generalised into a **SaaS product** so other lenders can use it too.

The product covers the full lifecycle of an asset-backed loan:

- Record a pledge: borrower KYC, item description, weight, metal, loan
  amount, monthly interest rate, optional address-proof image and
  proof-of-lending video.
- Track interest accrual (calendar months — incomplete month counts as a
  full month, minimum 1).
- Set a **probable close date** and a **hard deadline** (after which the
  pledged asset can be confiscated / melted to recover the loan).
- Close a case, recording amount received and an optional
  proof-of-closure video.
- Or flag a case as **bad debt** if recovery is no longer expected.
- View a rich **analytics dashboard**: KPIs, monthly trends, recovery
  rates, oldest open cases, overdue counts, top borrowers, etc.

Lenders sign up self-serve with **mobile OTP**, get a **7-day free
trial**, then upgrade to monthly / half-yearly / yearly plans. Payments
flow through a **gateway-agnostic** layer — currently configured for
"manual" (admin records cash / UPI / bank transfer), with stub
implementations ready for Razorpay, Stripe, and PayU.

---

## 2 · Architecture at a glance

```
                  ┌─────────────────────────────────────────────┐
                  │                Browser SPA                  │
                  │   React 18 (CDN) + Babel + Chart.js (CDN)   │
                  │   Single static file: static/index.html     │
                  └────────────────────┬────────────────────────┘
                                       │ JSON over HTTPS
                                       ▼
                  ┌─────────────────────────────────────────────┐
                  │              Flask app (app.py)             │
                  │  • Auth: sessions + password hashing        │
                  │  • OTP via SMSProvider (Console/Twilio/…)   │
                  │  • Billing via PaymentProvider (Manual/…)   │
                  │  • Multi-tenant scoping on every query      │
                  │  • Audit log on every sensitive action      │
                  └────────────────────┬────────────────────────┘
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                      ▼
   ┌──────────────────┐                                  ┌──────────────────┐
   │ Local: SQLite    │                                  │ Prod: PostgreSQL │
   │  lending.db      │                                  │  on Render       │
   └──────────────────┘                                  └──────────────────┘
```

- **Single-file Python backend** (`app.py`, ~1700 lines) — keeps deploys
  trivial; no extra modules to package.
- **Single-file frontend** (`static/index.html`, ~3000 lines) — no npm,
  no build step. Babel standalone compiles JSX in-browser. Drastically
  cuts deployment & maintenance complexity for a one-person shop.
- **Tenant isolation** by `user_id` column on the `cases` table, enforced
  at the SQL layer in every endpoint.

---

## 3 · Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.14 + Flask 3 + Gunicorn | Lightweight, well-known |
| DB driver | `psycopg[binary]==3.3.4` (psycopg3) | Required for Python 3.14 |
| PDF | `reportlab==4.2.5` | Receipt generation |
| 2FA  | `pyotp==2.9.0` | TOTP for admin login |
| Database (prod) | PostgreSQL on Render free tier | Persistent, managed |
| Database (dev) | SQLite (`lending.db`, auto-created) | Zero setup |
| Auth | Flask sessions + `werkzeug.security` (pbkdf2:sha256) | Simple, secure |
| Frontend | React 18 via CDN + Babel standalone | No build tooling |
| Charts | Chart.js 4.4.0 via CDN | Free, ergonomic |
| Hosting | Render (auto-deploy on `git push`) | Cheap, fast |
| Domain | prabhuventures.in via GoDaddy → Render | — |

---

## 4 · Roles & access model

The platform has **two roles**:

| Role | Who | Capability |
|---|---|---|
| `admin` | Prabhu Ventures staff | Sees all tenants, all leads, all payments, audit log, plan editor, admin manager. Single seeded account: **admin / admin123**. |
| `user` | Lender (tenant) | Sees only their own cases. Subscribes via billing page. Sign-up & sign-in via phone OTP. |

> A previous iteration also had a `super_admin` role; it was merged into
> `admin` to simplify ops. The role string survives in the DB schema as a
> permitted CHECK constraint value, so reintroducing the separation in
> future is a one-decorator change.

### OTP & founder backdoor
- Lender phone OTP is sent via the configured `SMS_PROVIDER`.
- A single hard-coded backdoor is scoped to the founder: phone
  `9479913772` + code `947200` always works regardless of any sent OTP.
  Override via `FOUNDER_PHONE` / `FOUNDER_OTP` env vars; disable
  entirely by setting `FOUNDER_OTP=""`.

---

## 5 · Feature set (current)

### Public landing page
- Hero with animated gradient + floating glass-panel "live dashboard" mock
- Stats counters that count up on scroll
- 6-card feature grid · 3-step how-it-works · about-us · CTA · footer
- Scroll-reveal animations, hover lifts, mobile responsive, respects
  `prefers-reduced-motion`
- Glassmorphic scroll-aware nav

### Auth modal (3 tabs)
- **Sign Up**: phone → OTP → name → 7-day trial starts, logged in
- **Sign In**: phone → OTP, logged in
- **Admin**: username + password (admins only)

### Tenant (lender) experience
- Subscription banner showing trial countdown, renewal warnings, or
  expired lock-out
- Home page with 5 action cards (Add New / View All / Add Existing /
  Dashboard / Billing)
- **Add New Case** form: borrower KYC, mobile, address & address-proof
  image, pledged item, weight, metal, loan amount, interest rate, date,
  time, **probable close month**, **hard deadline**, lending-proof video
- **Add Existing Case**: same form, but loan date/time entered manually
- **View All Cases**: powerful filters (search, metal, status, date
  range, amount exact/min/max), clickable rows
- **Case Detail Modal**: full record + media + interest calculation
  + close-the-case flow with closing-proof video + bad-debt marking
- **Dashboard & Analytics**: KPI cards, line/bar/donut charts, monthly
  trend, oldest-open cases, top borrowers, age buckets, weight by metal,
  overdue count
- **Billing**: current plan, plans grid, payment history
- Expired tenants are auto-redirected to billing and case endpoints
  return HTTP 402

### Admin experience
- KPI strip (lenders, active subs, trial subs, cases, revenue, leads)
- **Overview** tab — four charts (monthly revenue, signups, sub-status
  donut, plan-counts donut + revenue-by-plan bar)
- **Lenders** tab — list of tenants with subscription details, edit
  modal (name/phone/email + adjust subscription by ± days), suspend/
  activate, manual-payment recording
- **Admins** tab — list of admin accounts + create-new-admin modal
- **Plans** tab — full plan CRUD with active/hidden toggle
- **Signup Requests** tab — the lead capture queue from the landing page
- **Payments** tab — global payment log with status badges
- **Audit Log** tab — every login, signup, case action, payment,
  suspend/activate, plan change, sub extension, etc.

### Cross-cutting
- **Dark / light theme** with full token system (`ThemeCtx`),
  CSS-variables synced to `<html data-theme>` and localStorage persisted
- **i18n scaffolding** in 16 languages (English + 15 Indian) with
  `LangCtx` and `t()` helper — currently ~40 keys translated; full
  coverage is pending (Phase 3 todo)
- **Logo** rendered everywhere via a single `<Logo>` component with
  size-proportional rounded corners and optional glow

---

## 6 · Data model

> SQLite and PostgreSQL DDL are kept in parallel inside `init_db()`.
> Migrations are idempotent `ALTER TABLE ADD COLUMN IF NOT EXISTS` (PG)
> / try-except (SQLite).

| Table | Purpose | Key columns |
|---|---|---|
| `users` | All accounts (admin + tenants) | `id, name, phone, username, email, password_hash, role, status, aadhaar, pan, dob, address, business_name, kyc_status` |
| `cases` | The core loan ledger, **tenant-scoped** | `id, user_id (FK), name, father_name, address, mobile, items, weight, metal, money_lent, interest_rate, loan_date, loan_time, notes, status (open/closed/bad_debt), closed_at, amount_received, probable_close_date, hard_deadline, address_proof, lending_video, closing_video, created_at` |
| `plans` | Billing plans | `code, name, price_inr, duration_days, is_trial, active` |
| `subscriptions` | Tenant subscription history | `user_id, plan_code, status, started_at, expires_at` |
| `payments` | All payment attempts (provider-agnostic) | `user_id, subscription_id, plan_code, amount_inr, provider, provider_order_id, provider_payment_id, status, method, paid_at` |
| `otp_codes` | Issued OTPs (signup / login) | `phone, code, purpose, expires_at, used` |
| `leads` | Landing-page "request access" submissions | `name, email, phone, company, message, created_at` |
| `audit_log` | Append-only event log | `actor_id, action, target_type, target_id, meta (JSON), created_at` |
| `case_history` | Per-case change log (created / updated / closed / marked_bad_debt / renewed / partial_payment / reminder_sent) | `case_id, actor_id, action, changes (JSON diff), created_at` |
| `partial_payments` | Partial repayments against open cases | `case_id, amount, paid_at, method, note, actor_id` |
| `branches` | Per-tenant branches / locations | `user_id, name, address, phone, is_default` |

Existing legacy cases (from before multi-tenancy) are auto-migrated to a
**founding lender** account on `init_db()` so the live ledger is never
lost.

---

## 7 · HTTP API surface

### Public
- `POST /api/request-access` — landing page lead capture
- `GET  /api/gold-rate` — live 24k/22k/18k INR/g (cached 1 h, hard fallback)
- `POST /api/auth/request-otp` — `{ phone, purpose: signup|login }`
- `POST /api/auth/verify-otp` — `{ phone, code, name?, purpose }`
- `POST /api/auth/login` — username + password (admins / legacy)
- `POST /api/auth/logout` · `GET /api/check-auth` · `GET /api/auth/me`
- `GET  /api/billing/plans` — public plan listing

### Tenant (role: user)
- `GET/POST /api/cases` · `GET /api/cases/<id>`
- `PATCH /api/cases/<id>` — edit case (diff captured to `case_history`)
- `DELETE /api/cases/<id>` — permanent delete (audit retained)
- `GET /api/cases/<id>/history` — chronological per-case history
- `POST /api/cases/<id>/close` · `POST /api/cases/<id>/bad-debt`
- `GET /api/dashboard`
- `GET /api/users/me/profile` — own profile + subscription + payments
- `PATCH /api/users/me` — edit own profile / KYC (name, email, address, business_name, aadhaar, pan, dob)
- `GET /api/borrowers` · `GET /api/borrowers/<phone>/cases` — borrower-grouped views
- `POST /api/cases/<id>/renew` — extend deadline, optionally capitalise interest
- `POST /api/cases/<id>/partial-payment` · `GET /api/cases/<id>/partial-payments`
- `GET /api/cases/<id>/receipt?type=lending|closure` — PDF download
- `GET/POST /api/branches` · `DELETE /api/branches/<id>` — branch CRUD
- `POST /api/auth/2fa/setup` · `/verify` · `/disable` — TOTP for admins
- `POST /api/auth/login-totp` — second step of 2FA login
- `GET /api/billing/me` · `POST /api/billing/create-order` · `POST /api/billing/verify`

### Admin
- `GET /api/admin/stats`, `/charts`, `/users`, `/admins`, `/plans`, `/payments`, `/audit`, `/leads`
- `PUT /api/admin/users/<id>`
- `POST /api/admin/users/<id>/suspend|activate`
- `POST /api/admin/users/<id>/kyc` — `{ status: verified|pending|rejected }`
- `POST /api/admin/subscription/<user_id>/extend` — `{ days }`
- `POST /api/admin/create-admin`
- `POST /api/admin/plans` — upsert plan
- `POST /api/admin/send-reminders` — sweep open cases & send SMS (cron-friendly)
- `POST /api/billing/manual-payment` — record offline payment

All authenticated endpoints are gated by `@require_auth(roles=[...])`.
Tenant case/dashboard endpoints are additionally gated by
`@require_active_subscription`, which returns HTTP **402 SUB_EXPIRED**
when the lender's sub is expired.

---

## 8 · Frontend architecture

Everything lives in a single file: `static/index.html`. Top-down:

1. CSS variables for light/dark + animation keyframes
2. React imports (CDN) + helper utilities (`fmtCompact`, `calcMonths`,
   `calcInterest`, etc.)
3. **Contexts**: `ThemeCtx` (`{ C, dark, toggle }`) and `LangCtx`
   (`{ lang, setLang, t }`)
4. **`<Logo>`** brand component with size-proportional rounded corners
5. **UI primitives**: `Field`, `Input`, `Select`, `Textarea`, `Btn`,
   `Alert`, `Navbar`, `Card`, `Modal`, `StatusBadge`, `FileUpload`,
   `KpiCard`, `ChartBox`, `StatChip`, `InfoRow`
6. **Chart wrappers**: `ChartCanvas` (with `themeKey` to force re-render
   on theme switch), `BarChart`, `LineChart`, `DoughnutChart`
7. **Pages**:
   - `LandingPage` (hero, features, how-it-works, about, contact, footer)
   - `AuthModal` (Sign Up / Sign In / Admin tabs)
   - `HomePage` (tenant landing — 5 cards)
   - `AddCasePage` (parameterised by `isExisting`)
   - `ViewCasesPage` + `CaseDetailModal`
   - `DashboardPage` (analytics)
   - `BillingPage`
   - `SubBanner` (trial / expiry)
   - `AdminConsole` (7 tabs, modals)
8. **`<App>`** root — fetches `/api/auth/me`, routes by role, persists
   lang & theme to localStorage, force-redirects expired tenants to
   billing.

---

## 9 · Local development

```bash
cd /Users/aman/lending-app
python3 app.py
# Open http://localhost:8080
```

Banner on startup prints all seeded credentials and provider config.
Default behaviour:

- **DB**: SQLite at `./lending.db` (auto-created, auto-migrated)
- **SMS provider**: `ConsoleSMSProvider` — OTPs print to stdout
- **Payment provider**: `ManualPaymentProvider` — orders auto-verify
- **Admin login**: `admin / admin123`
- **Founder lender**: phone `9479913772`, OTP `947200`

> On macOS, port 5000 is blocked by AirPlay Receiver, so we use 8080.

---

## 10 · Deploying

Render auto-deploys on every push to `main`. Process:

```bash
cd /Users/aman/lending-app
git add .
git commit -m "concise message"
git push
```

SSH key at `~/.ssh/github_key`, configured in `~/.ssh/config` — no
password prompts. Render rebuild + restart usually completes within
~2 minutes; hard-refresh (Cmd+Shift+R) to bust browser cache.

**Never** force-push to `main`. **Always** keep `init_db()` at module
level so Gunicorn runs it on startup.

---

## 11 · Environment variables

Configured per environment on Render. None are mandatory; sensible
defaults ship in `app.py`.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | If set, switches to PostgreSQL. Render injects this automatically. |
| `SECRET_KEY` | dev key | Flask session secret. Set in prod. |
| `ADMIN_USERNAME` | `admin` | Bootstrap admin username |
| `ADMIN_PASSWORD` | `admin123` | Bootstrap admin password |
| `FOUNDER_PHONE` | `9479913772` | Lender account auto-created on first deploy |
| `FOUNDER_NAME` | `Aman` | Display name for the founder |
| `FOUNDER_OTP` | `947200` | OTP backdoor for `FOUNDER_PHONE`. Set to `""` to disable. |
| `SMS_PROVIDER` | `console` | One of `console`, `twilio`, `msg91` |
| `PAYMENT_PROVIDER` | `manual` | One of `manual`, `razorpay`, `stripe`, `payu` |

---

## 12 · Provider abstractions

The two integration surfaces — SMS and payments — are gateway-agnostic
behind simple Python interfaces inside `app.py`.

```python
class SMSProvider:
    def send(self, phone: str, message: str) -> bool: ...

class PaymentProvider:
    def create_order(self, amount_inr, currency, metadata) -> dict: ...
    def verify_payment(self, payload: dict) -> dict: ...
    def refund(self, provider_payment_id, amount_inr=None) -> dict: ...
```

Working implementations:
- `ConsoleSMSProvider` — prints OTPs to logs. Good for dev / debug.
- `ManualPaymentProvider` — generates an order ID, admin marks payment
  received later via the manual-payment endpoint.

Stub implementations (raise `NotImplementedError` until configured):
- `TwilioSMSProvider`, `MSG91SMSProvider`
- `RazorpayProvider`, `StripeProvider`, `PayUProvider`

Switching is one env var. No code change.

---

## 13 · Repository layout

```
lending-app/
├─ app.py                 ← Flask backend (everything)
├─ requirements.txt       ← Flask, Gunicorn, psycopg
├─ runtime.txt            ← Python version hint (Render ignores; runs 3.14)
├─ Procfile               ← `web: gunicorn app:app`
├─ README.md              ← This file
├─ docs/
│  └─ SCALING.md          ← Architecture & cost model for 1 Cr tenants
├─ lending.db             ← Local SQLite (gitignored)
└─ static/
   ├─ index.html          ← React SPA (single file)
   └─ logo.png            ← Brand logo (square, ~234 KB)
```

---

## 14 · Project history (chronological)

A curated record of every meaningful commit since inception. Each new
commit going forward will append a row here.

| Date | Commit | Summary |
|---|---|---|
| 2026-05 | initial | Flask + SQLite skeleton, login, basic case CRUD |
| 2026-05 | move-init-db | `init_db()` lifted to module level so Gunicorn runs it |
| 2026-05 | postgres-switch | Switched to PostgreSQL via `DATABASE_URL` (SQLite fallback) |
| 2026-05 | psycopg3 | Pinned `psycopg[binary]==3.3.4` for Python 3.14 |
| 2026-05 | runtime-pin | `runtime.txt` set to Python 3.11 (later abandoned — Render ignores) |
| 2026-05 | add-new-case-icon | Replaced 🏦 emoji with white "+" on Add New Case card |
| 2026-05 | six-features | Case closing, "Add Existing", address field, exact amount search, clickable rows, status filter |
| 2026-05 | dashboard-bad-debt | Full Dashboard & Analytics page + bad-debt status |
| 2026-05 | mobile-image-video-fields | Mobile number, address proof, lending video, hard deadline, probable close date |
| 2026-05 | google-translate | Initial i18n via Google Translate widget |
| 2026-05 | gt-style-fix · gt-style-tag | CSS fixes for Translate widget |
| 2026-05 | builtin-langselector | Replaced Google Translate with native `<select>` and hand-curated translations in 16 languages (~40 keys) |
| 2026-05 | logout-everywhere | Logout button on every page; white arrow on language selector |
| 2026-05 | landing-page | World-class Prabhu Ventures landing page (hero, charts mockup, features, CTAs) |
| 2026-05 | saas-foundation | **Phase 1**: roles + multi-tenancy + OTP signup + subscriptions + payment abstraction + audit log |
| 2026-05 | pg-bool-fix | PG boolean type mismatch fix in plan seed |
| 2026-05 | founder-phone | Founder phone → 9479913772; universal DEV_OTP backdoor (later scoped) |
| 2026-05 | founder-otp-scope | Backdoor scoped to founder phone only |
| 2026-05 | admin-rich-ui | **Phase 2**: Overview charts, lender editor, plan CRUD, admin manager, audit viewer |
| 2026-05 | merge-roles | `super_admin` merged into `admin` (single staff role) |
| 2026-05 | logo-base | Added `<Logo>` component and replaced emoji at 4 sites |
| 2026-05 | logo-compress | 10 MB → 234 KB; cropped to square 512×512 centred on PV |
| 2026-05 | logo-flat | Removed radial-fade mask; render as plain `<img>` |
| 2026-06 | logo-rounded | Rounded corners (~22% radius, size-proportional) |
| 2026-06 | readme-init | **This** README with full project history |
| 2026-06 | case-edit-history-delete | Case edit + per-case history table + collapsible View History accordion + delete with permanent-deletion warning. New `case_history` table, `PATCH /api/cases/<id>`, `DELETE /api/cases/<id>`, `GET /api/cases/<id>/history`. |
| 2026-06 | profile-menu | Replaced Logout button with avatar/profile menu. Tenants get a full slide-out ProfilePanel (profile + KYC + billing + payment history + preferences + logout). Admins get a clean dropdown (preferences + logout). Landing page settings dropdown for lang + theme. Date strip + Billing card removed from HomePage. New KYC columns on users, `GET /api/users/me/profile`, `PATCH /api/users/me`, Customer ID format `PV-NNNNNN`. |
| 2026-06 | scaling-doc | Added `docs/SCALING.md` — full architecture + cost model for scaling to 1 crore (10 M) tenants. Phased migration plan, target architecture, per-component deep dives, capacity model, ~$220 – 380 K/mo cost ballpark. Pure docs, no code changes. |
| 2026-06 | gold-rate | Live gold-rate widget. New `GET /api/gold-rate` endpoint pulls 24k/22k/18k INR/g from a free public spot-price API (USD/oz × USD-INR ÷ 31.1035), 1-hour server cache, hard fallback on API failure. New `<GoldRateBar>` shown on Add New Case + Dashboard. Add-case shows a "Suggested loan @ 75% LTV" hint with one-click apply when metal=Gold and weight is filled. |
| 2026-06 | phase3-bundle | **8 Phase-3 features shipped together (#5b–#5i)**: borrower profile (`GET /api/borrowers`, `/api/borrowers/<phone>/cases`, history hint on Add Case form); loan renewal (`POST /api/cases/<id>/renew` with deadline push + optional interest capitalisation); partial payments (new `partial_payments` table, `POST/GET /api/cases/<id>/partial-payment[s]`, ledger inside case modal); PDF receipts via reportlab (`GET /api/cases/<id>/receipt?type=lending\|closure`); 2FA for admins via pyotp (`POST /api/auth/2fa/{setup,verify,disable}`, two-step login at `/api/auth/login-totp`); KYC admin verify/reject (`POST /api/admin/users/<id>/kyc`, dropdown action on Lenders tab); multi-branch (`branches` table, `GET/POST /api/branches`, `DELETE /api/branches/<id>`, manage inside ProfilePanel); SMS reminders (`POST /api/admin/send-reminders` — finds open cases with deadline ≤7d or overdue, sends via configured SMS provider). |
| 2026-06 | i18n-expand | Major translation expansion (#1). TR map grown from ~40 to **103 keys × 16 languages** (≈1,650 translation strings). Landing page hero, badges, feature/about/CTA sections, footer, "How it works" steps, AuthModal headings, ProfilePanel sections, AdminConsole tabs all translate live with language switch. Remaining English fallbacks: long descriptive paragraphs in feature cards, error toasts, and admin-only labels. |
| 2026-06 | hooks-fix | Fixed a React Rules of Hooks crash (`useState`/`useEffect` declared after an early return in `App`) that left the site blank with only the document title visible. |
| 2026-06 | remove-i18n-ui | **Pulled the language switcher out of the MVP** by user request — translation quality wasn't as expected. `LangCtx.Provider` removed, language UI rows stripped from Navbar / LandingNav / ProfilePanel, `LangSelector` neutralised, `lang`/`setLang` plumbing removed from App. The `TR` map and `useLang()` stub stay so all existing `t('key')` calls continue to render the English value — re-introducing i18n later is a one-day change. |

---

## 15 · Phasing & roadmap

We are building this in deliberate phases to ship working software at
every step.

### ✅ Phase 1 (done) — SaaS foundation
Roles, mobile OTP, multi-tenancy, subscriptions, payment provider
abstraction, audit log, founder seeding.

### ✅ Phase 2 (done) — Admin console
Overview with charts, lender editor with sub-extension, plan CRUD,
admin manager, audit-log viewer.

### 🚧 Phase 3 (in progress) — Domain features
Tracked as separate user requests; expected to span several sessions.

- [x] Rounded logo corners
- [x] README & history tracking
- [x] Edit case + audit history + delete with confirmation
- [x] Profile menu (KYC + billing inside, logout at bottom)
- [x] Language + theme moved into profile / settings menus
- [x] 1 Cr scaling architecture doc — see [docs/SCALING.md](docs/SCALING.md)
- [ ] ~~Live gold rate widget~~ — removed from MVP (frontend bar + suggested-loan helper + backend `/api/gold-rate` all gone)
- [x] Borrower profile (linked cases by phone, history hint on Add Case)
- [x] Loan renewal / extension (deadline + optional interest capitalisation)
- [x] Partial payments (record + ledger in case modal)
- [x] PDF receipts (lending + closure, signed-style A4 layout)
- [x] 2FA for admins (TOTP via authenticator app)
- [x] Formal KYC capture + admin verify/reject/pending workflow
- [x] Multi-branch support (manage branches inside profile panel)
- [x] SMS reminders (admin-triggered sweep for 7-day deadlines + overdue)
- [ ] ~~i18n / multi-language~~ — removed from MVP, will revisit later
- [ ] Move language + theme controls into the profile menu
- [ ] 1 Cr scaling architecture doc
- [ ] Live gold rate widget
- [ ] Borrower profile (linked cases by phone)
- [ ] PDF receipts
- [ ] Loan renewal / extension
- [ ] Partial payments
- [ ] 2FA for admins
- [ ] Formal KYC capture (Aadhaar / PAN)
- [ ] Multi-branch support
- [ ] SMS reminders (scheduled cron)

---

## 16 · Contributing notes for future Claude/dev sessions

If you're a Claude session picking this up, here's what's important:

1. **Single-file philosophy.** Keep backend in `app.py` and frontend in
   `static/index.html` unless there's a strong reason to split. Render
   deploys benefit hugely from this.
2. **Database compatibility.** All DDL & DML must work on both SQLite
   (local) and PostgreSQL (prod). Use `db_execute()` which translates
   `?` → `%s` automatically. Boolean values: pass `True/False` on PG,
   `1/0` on SQLite (`USE_PG` ternary).
3. **Migrations.** Use `ADD COLUMN IF NOT EXISTS` (PG) or wrap in
   try/except (SQLite). Never drop columns; data migration must preserve
   live production rows.
4. **Tenant scoping.** Any new tenant-facing endpoint must filter by
   `user_id` for `role=user`. Admin endpoints bypass.
5. **Audit log.** Call `audit(action, target_type, target_id, meta)` on
   every sensitive write.
6. **Provider abstractions.** Never call Razorpay / Twilio / etc. SDKs
   directly. Always go through `get_payment_provider()` /
   `get_sms_provider()`.
7. **Frontend conventions.** Use `useTheme()` and `useLang()` for colors
   and translations. Reuse `Btn`, `Input`, `Modal`, etc. Don't reach
   for new chart libraries; the wrappers around Chart.js suffice.
8. **i18n.** When adding a new label, also add an English entry to the
   `TR` map. The 15 Indian-language translations can be filled in once
   per major release.
9. **No build step.** No npm, no webpack. JSX is compiled in-browser by
   Babel standalone. Keep it that way unless you intentionally split.
10. **Commit hygiene.** Each commit message: concise first line,
    optional bullet body, always co-author footer for AI-written code.
    Update the **Project history** table in this README in the same
    commit.
11. **Never push to prod blindly.** Always run `python3 -c "import app"`
    and ideally a quick `test_client` smoke before pushing.
12. **Never force-push** to `main`.

---

| 2026-06 | nav-theme-out + profile-redesign | (a) Pulled dark-mode toggle out of profile/preferences and made it a standalone circular button visible in every navbar (tenant, admin, and landing page). Landing-page "⚙ Settings" dropdown removed entirely. (b) Re-designed `ProfilePanel` in a Zepto-style accordion layout: soft gradient hero card with avatar + Customer ID + live subscription chip, collapsible icon-led sections (👤 Profile · 🪪 KYC · 💳 Subscription · 📜 Payments · 🏢 Branches), animated chevrons, default-open Profile section, summary previews when collapsed, modern pill-shaped status chips, prominent red Logout pinned at the bottom. Preferences section removed since theme is now in the navbar. |

| 2026-06 | footer-everywhere + remove-gold | (a) Added a shared `<Footer />` component shown on every authenticated page: HomePage, AddCasePage, ViewCasesPage, DashboardPage (incl. loading/error states), AdminConsole. Same copyright row format as the landing page. Each page wrapped in a flex column so the footer pins to the viewport bottom. (b) Removed the gold-price feature entirely — `GoldRateBar`, `GoldRateProvider`, `useGoldRate` hook + Add-Case "Suggested loan @ 75% LTV" helper + Dashboard gold ticker all gone from the frontend. Backend `GET /api/gold-rate`, `fetch_gold_rate()`, `_GOLD_CACHE`/`_GOLD_FALLBACK`/`_USD_INR` constants, and the `urllib.request` import all removed. |

| 2026-06 | profile-compact | Re-redesigned ProfilePanel as a Groww-style compact list. Slim header (40 px avatar + name + customer-id + close button), thin contact strip (phone + mini sub-chip), single-card body with hairline-divided list rows instead of separate cards, smaller icons (30 px), tighter typography (13.5 / 11.5 px), padding cut roughly in half across the board. Logout is now a ghost outline button instead of a full red bar. Panel width reduced from 460 → 380 px. |

_Last updated: 2026-06-01 — commit `profile-compact`_
