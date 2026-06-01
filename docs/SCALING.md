# Scaling LoanTrack to 1 Crore Tenants

**Goal:** Support **10 million lender accounts** (tenants) — each with their
own loan book of up to ~1,000 cases, full media attachments, real-time
dashboards, and 24×7 availability.

This document is a working architecture & cost plan. It is intentionally
opinionated and pragmatic — the goal is "what would I actually build / pay
for if this lands tomorrow."

> _**Tenant** = a lender business who pays Prabhu Ventures a subscription._
> _**Borrower** = end-customer of the tenant, not a system account._

---

## 1 · Executive summary

| | Today | At 10 M tenants |
|---|---|---|
| Tenants | ~1 | 10,000,000 |
| Cases (data rows) | ~10s | ~5 billion |
| Media (images + video) | ~MBs | **~50 – 150 petabytes** |
| Concurrent requests (peak) | ~1 | **20 – 30 K RPS** |
| Daily API requests | ~100 | **3 – 5 billion** |
| Compute | 1 Render web dyno | **80 – 150** stateless containers |
| Database | 1 Postgres instance | **64-shard Postgres / Aurora / Spanner** |
| Storage backend | Postgres `TEXT` (base64) | **S3 + CloudFront / GCS + Cloud CDN** |
| Search & analytics | Live Postgres scans | **OpenSearch + ClickHouse** |
| Background work | Inline in request | **SQS / Cloud Tasks + worker fleet** |
| Auth state | Flask session in cookie | **Redis cluster** |
| Cost (monthly) | **~$10** (Render free / hobby) | **~$220 K – 380 K** (AWS) |

The architecture below survives until at least **20 – 30 M users** with
horizontal scale-out only — no further rewrites. Most one-way decisions
(media → S3, shard key, Redis-backed sessions) are taken explicitly so they
don't need to be revisited.

---

## 2 · Hard bottlenecks in today's design

Honest list of what would break first. In rough order:

1. **Base64 media in Postgres** — `address_proof`, `lending_video`,
   `closing_video` columns. A single bloated row makes queries 100× slower.
   Postgres `TOAST` saves us for now; at scale it kills index efficiency
   and backup size. **One-way fix: move all media to object storage.**
2. **Single Postgres instance.** Render's free tier maxes at small RAM/IO.
   By ~50 K active tenants we'd be on a $1 – 2 K/mo Render PG plan; by
   ~5 M tenants we'd need read replicas + sharding.
3. **No connection pooling.** Each Gunicorn worker opens its own DB
   connection. With 100 workers across 10 dynos that's 1,000 connections;
   most Postgres instances cap at ~500. **Need PgBouncer.**
4. **Session cookies hold no server state.** Good for stateless scale-out
   but bad for token revocation. We need **Redis-backed sessions** for
   admin force-logout, OTP storage with TTL, and rate limiting.
5. **Synchronous request work**: dashboard endpoint runs ~10 SQL queries
   inline; image upload is base64-decoded inline; SMS goes through the
   request thread. **Anything > 50 ms must move to a worker.**
6. **Render free-tier inactivity sleep** — first request after 15 min
   idle takes ~30 s. Acceptable for personal use; lethal for paying
   customers. **Must move to a paid plan before any real traffic.**
7. **OTPs in Postgres.** A 6-digit code with 10-minute TTL is exactly the
   workload Redis was built for. Postgres works but burns IOPS.
8. **Audit log size.** At 10 M tenants writing 100 actions/day each,
   that's a billion rows per day. Postgres OK for ~3 months; then needs
   ClickHouse or S3 + Athena.
9. **Single-region deploy.** P99 latency from US-East to India is ~250 ms.
   Move to ap-south-1 + CDN.
10. **No payment-provider integration.** ManualPaymentProvider does not
    scale to 10 M billing events; need real Razorpay/Stripe at volume.

---

## 3 · Target architecture

```
                                  ┌─────────────────────────────────┐
   prabhuventures.in              │     CloudFront / Cloud CDN      │
   (anycast DNS — Route 53)──────▶│  static assets · cached APIs    │
                                  └────────────────┬────────────────┘
                                                   │
                                                   ▼
                                   ┌──────────────────────────────┐
                                   │   Application Load Balancer  │
                                   │     (ALB / NLB, region-aware) │
                                   └─┬────────────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌────────────┐         ┌────────────┐         ┌────────────┐
       │  Flask app │   ...   │  Flask app │   ...   │  Flask app │
       │  Gunicorn  │         │  Gunicorn  │         │  Gunicorn  │
       │  (ECS Fgte)│         │  (ECS Fgte)│         │  (ECS Fgte)│
       └─────┬──────┘         └─────┬──────┘         └─────┬──────┘
             │                      │                       │
             │       ┌──────────────┴───────────────────────┘
             │       │
             ▼       ▼
       ┌─────────────────────┐         ┌──────────────────────────┐
       │   PgBouncer pool    │         │ Redis Cluster (ElastiCache)│
       │ (connection broker) │         │  • sessions               │
       └──┬──────────────────┘         │  • OTPs                   │
          │                            │  • rate-limit counters    │
          ▼                            │  • dashboard cache        │
   ┌────────────────────────┐          │  • feature flags          │
   │  Aurora Postgres       │          └──────────────────────────┘
   │  Global Database       │
   │  • 64 logical shards   │          ┌──────────────────────────┐
   │  • 1 writer + 5 readers│          │  OpenSearch / Elastic     │
   │    per shard           │◀────────▶│  • case search index      │
   └────────┬───────────────┘          │  • borrower search        │
            │                          └──────────────────────────┘
            │ CDC via Debezium
            ▼
   ┌────────────────────────┐          ┌──────────────────────────┐
   │  ClickHouse            │          │   S3 (ap-south-1)         │
   │  • audit log           │          │  • address-proof images   │
   │  • payment events      │          │  • lending videos         │
   │  • case_history        │          │  • closing videos         │
   │  • analytics queries   │          │  + Lifecycle to Glacier    │
   └────────────────────────┘          │     after 1 year           │
                                       └──────────────────────────┘
   ┌────────────────────────┐
   │   SQS / Cloud Tasks    │◀────────── Workers (Celery / Lambda)
   │  • OTP send            │              • SMS via Twilio / MSG91
   │  • PDF generation      │              • Email via SES
   │  • Email send          │              • Image resize + virus scan
   │  • Image processing    │              • Daily DB backups
   │  • Scheduled reminders │              • Subscription expiry job
   └────────────────────────┘
```

---

## 4 · Per-component deep dive

### 4.1 · Compute — Stateless Flask containers

- Containerise `app.py` + `static/` into a single Docker image (~150 MB).
- Run on **ECS Fargate** (zero-ops) or **EKS** (more control, more ops).
  GKE Autopilot is the GCP equivalent.
- Sizing: each container 2 vCPU / 4 GB RAM, 4 Gunicorn workers, 8 threads.
  Handles ~200 RPS sustained, 600 RPS burst.
- Auto-scale on CPU > 60% **and** ALB request count > 200 RPS per task.
- **80 – 150 tasks** at peak, **20 – 30** at trough.
- Health checks: `/api/check-auth` returns 200 fast.
- Multi-AZ; ALB does cross-zone routing.
- **Estimated cost:** $4 – 8 K / mo at peak average usage (Fargate Spot
  brings this down by 60% for stateless workloads).

### 4.2 · Database — Sharded Aurora Postgres

The single biggest decision. Options ranked:

1. **Sharded Aurora Postgres (recommended).** Stick with what we know,
   shard by `tenant_id % 64` (or by hash). Each shard is a separate
   Aurora cluster: 1 writer + 5 readers, db.r6g.4xlarge (32 vCPU, 128 GB).
   Cross-shard queries are rare (admin analytics only — do them on
   ClickHouse instead).
2. **CockroachDB / Spanner** — automatic sharding, but unfamiliar SQL
   semantics and 3 – 5× the cost of Aurora.
3. **Single huge Aurora** — works up to ~1 M tenants. Buys time but is
   not a 10 M solution.

**Sharding plan:**

- Routing layer in Python: a small `db_for(tenant_id)` helper picks the
  correct connection. Hidden from endpoint code by a context manager.
- Tenants table (the catalogue of which tenant lives on which shard)
  lives on a small "control plane" cluster.
- New tenant signup picks the shard with lowest load.
- Tenant migration (re-sharding) uses logical replication.

**Why 64 shards?** At 10 M tenants × 5 KB metadata per tenant = 50 GB of
hot metadata, plus 5 B cases × ~2 KB metadata (post-media-extraction) =
10 TB total. 64 shards × ~160 GB each comfortably fits in the
db.r6g.4xlarge sweet spot.

**Index strategy:**
- Composite indexes: `(user_id, status, id DESC)` for case listings.
- Partial indexes: `WHERE status = 'open'` — most queries hit only opens.
- BRIN on `created_at` for audit / history time-range queries.
- Avoid GIN on JSON columns at this scale — use OpenSearch instead.

**Estimated cost:** **$80 – 120 K / mo** for 64 sharded Aurora clusters
in ap-south-1. This is the biggest line item.

### 4.3 · Caching — Redis cluster

ElastiCache Redis cluster, 6 shards × 3 nodes each, cache.r6g.large.

| What we cache | TTL | Reason |
|---|---|---|
| Session → user_id | 24 h sliding | Replaces Flask cookie-based session |
| OTP codes (key = phone) | 10 min | Burns no Postgres IOPS |
| `/api/auth/me` per session | 30 s | Cuts the per-request user lookup |
| `/api/billing/me` per user | 60 s | Sub state changes infrequently |
| `/api/dashboard` per tenant | 5 min | Expensive aggregation |
| Rate-limit counters | sliding | Stops abuse on /request-otp |
| Plan list | 1 h | Hot global cache |
| Feature flags | 30 s | Stop-the-world toggles |

Sessions in Redis also unlock **force-logout** (admin can revoke a
tenant's tokens) and **single-sign-out across devices** — neither works
with cookie-only sessions.

**Estimated cost:** $4 – 6 K / mo.

### 4.4 · Storage — S3 + CloudFront

Today every image and video is base64-encoded into a Postgres TEXT
column. **Stops being viable at 50 K tenants.** Migration plan:

1. Add columns `address_proof_key`, `lending_video_key`,
   `closing_video_key` to `cases`.
2. New uploads: server returns a **presigned PUT URL**, client uploads
   direct to S3, then POSTs the key back.
3. Backfill job moves existing base64 blobs out to S3.
4. Delete the old TEXT columns once backfill verified.
5. CloudFront in front of S3 with signed URLs for private access.

Per-bucket lifecycle: hot for 30 days → Standard-IA for 1 year → Glacier
Deep Archive after that. Reduces 70% of storage cost.

**Estimated cost:** At 1 PB hot + 50 PB Glacier = **$25 – 40 K / mo**
storage + egress. Dominated by egress, so CDN edge caching is critical.

### 4.5 · Search & analytics — OpenSearch + ClickHouse

- **OpenSearch** for case search ("find all of borrower Ramesh") and
  dashboard filtering. Documents indexed via Debezium CDC from Aurora
  shards. ~10 r6g.2xlarge data nodes + 3 master nodes.
- **ClickHouse Cloud** for audit log, case_history, payment events,
  dashboard charts. Columnar storage = 100× compression vs Postgres for
  these workloads. Aggregation queries that take 5 s on Postgres run in
  60 ms on ClickHouse.

**Estimated cost:** OpenSearch ~$6 K / mo. ClickHouse Cloud ~$5 K / mo.

### 4.6 · Background workers & queues

Move all non-critical-path work off the request thread.

| Job | Trigger | Runtime | Provider |
|---|---|---|---|
| Send SMS OTP | API request | Lambda (Python) | SQS |
| Send transactional email | API request | Lambda | SQS |
| Generate PDF receipt | Case close | Lambda + ReportLab | SQS |
| Image resize + virus scan | S3 PUT event | Lambda | S3 → Lambda |
| Daily DB snapshots | EventBridge cron | Step Function | RDS API |
| Subscription expiry sweep | Hourly cron | Lambda | EventBridge |
| Hard-deadline reminders | Hourly cron | Lambda | EventBridge |
| Bulk lender export | Admin click | Fargate task | SQS |

**Estimated cost:** $3 – 5 K / mo (mostly Lambda invocation + SQS).

### 4.7 · Payments at scale

Move from `ManualPaymentProvider` to real gateways with the abstraction
layer already in place:

- **Razorpay** as primary for INR (already the dominant Indian gateway).
- **Stripe** as fallback for international payments / cards.
- **Webhook handler** dedicated to receiving async settlement events
  (idempotent, signed).
- **Reconciliation worker** runs nightly to match `payments` table
  against gateway settlements; flags drift.
- Payment data stays inside the main shard for ACID — never split.

PCI scope: stay **SAQ A** by using gateway-hosted checkout pages. Never
touch raw card data.

### 4.8 · Multi-region & failover

- Primary region: **ap-south-1** (Mumbai) — closest to user base.
- Hot standby: **ap-south-2** (Hyderabad) or **ap-southeast-1** (Singapore).
- **Aurora Global Database** for cross-region replication (~1 s lag).
- DNS failover via Route 53 health checks.
- RPO ≤ 5 s, RTO ≤ 5 min.

---

## 5 · Migration phases

We won't build any of this on day one. Each step ships when a real metric
forces it.

| Phase | Trigger | What we do | Effort |
|---|---|---|---|
| **0 — Today** | < 100 tenants | Render PG + Flask. Just run. | 0 |
| **1 — Move media to S3** | First tenant with > 1 GB of media | Cut base64 columns. Easy big-win refactor. | 2 weeks |
| **2 — Production paid Render plan** | First paying customer | No more sleep. PgBouncer addon. | 1 day |
| **3 — Postgres → Aurora (single)** | ~10 K active tenants | Lift-and-shift. Add 2 read replicas. | 1 week |
| **4 — Redis sessions + OTP cache** | ~100 K active tenants | Replace cookie sessions, move OTPs. Unlocks force-logout. | 1 week |
| **5 — OpenSearch for case search** | ~500 K active tenants | Postgres LIKE searches slow down. | 2 weeks |
| **6 — Worker queue** | ~500 K active tenants | Move SMS / email / PDF to SQS + Lambda. | 2 weeks |
| **7 — ClickHouse for audit/analytics** | ~1 M active tenants | Postgres audit/log tables become unmanageable. | 3 weeks |
| **8 — Aurora sharding** | ~5 M active tenants | Routing layer; one-time cut-over per shard. | 8 – 12 weeks |
| **9 — Multi-region** | First international wave | Aurora Global + DNS failover. | 4 weeks |

Critically, **none of phases 1 – 7 require schema or code rewrites that
break the current architecture** — they are all additive.

---

## 6 · Cost model (monthly, peak season)

| Line item | Cost |
|---|---|
| Compute (Fargate, 80 – 150 tasks) | $4 – 8 K |
| Aurora (64 shards, RIs) | $80 – 120 K |
| Redis cluster | $4 – 6 K |
| S3 storage + egress (1 PB hot) | $25 – 40 K |
| OpenSearch | $6 K |
| ClickHouse Cloud | $5 K |
| Workers (Lambda + SQS) | $3 – 5 K |
| CloudFront | $8 – 12 K |
| Twilio / MSG91 SMS (10 M users × 2 OTPs/mo × $0.003) | $6 K |
| Razorpay / Stripe fees | revenue-share, not infra |
| Monitoring (Datadog / Grafana Cloud) | $4 – 6 K |
| Logging (CloudWatch) | $5 – 8 K |
| Security (WAF, GuardDuty, Inspector) | $2 – 3 K |
| Multi-region buffer (10%) | $15 – 20 K |
| **Total infra** | **~$170 – 240 K / mo** |
| Engineering team (3 backend, 1 SRE, 1 data) | ~$80 – 130 K / mo |
| **Run cost** | **~$250 – 370 K / mo** |

**Per-tenant cost:** ~$0.025 / month at full scale. Even at the entry
"monthly" plan price of ₹999, gross margin is ~97%.

---

## 7 · Capacity model

Assumptions:

- 10 M registered tenants.
- 20% MAU = 2 M monthly active.
- 5% DAU = 500 K daily active.
- Each DAU makes 50 API calls (case CRUD + dashboards + media).
- Peak hour = 4× average → ~5,200 RPS sustained, 20 – 30 K peak burst.

The 80 – 150 Fargate-task fleet absorbs this with ~40% headroom. Aurora
sharded clusters handle ~30 K writes/sec aggregate, ~150 K reads/sec
with replicas — comfortably > 4× projected load.

---

## 8 · Risks & open questions

- **Sharding rebalance is irreversible once data ages**; we must add new
  shards via consistent-hash ring to avoid full data migrations every
  time we grow.
- **GDPR-style data subject deletion** is harder when audit data is in
  ClickHouse (compaction lag). Need an explicit erasure pipeline.
- **Aurora cross-region replication lag** spikes during regional failover.
  Documented RTO of 5 min may stretch to 10 in worst case.
- **SMS deliverability in India** depends heavily on TRAI DLT registration.
  Carrier-specific delivery rates vary widely — plan for fallback paths
  (Twilio → MSG91 → Gupshup).
- **KYC compliance**: at scale we likely need to integrate with a
  registered KYC provider (Hyperverge, IDfy, Karza). Aadhaar-based
  e-KYC is regulated; can't be a hand-rolled flow.
- **Cost optimisation potential** is huge — reserved instances + Fargate
  Spot can cut compute by 60 – 70%. Use Aurora I/O-Optimized only on
  hot shards.

---

## 9 · What this doc is _not_

- Not a "build it now" plan. We are at Phase 0. Most of the above is
  decision-deferred.
- Not a comprehensive compliance / security review. PCI, RBI guidelines,
  DPDP Act — each deserves its own doc.
- Not a vendor lock-in commitment. Every choice above has a GCP / Azure
  / Cloudflare equivalent.

---

_Last updated: 2026-06-01 — commit `scaling-doc`_
