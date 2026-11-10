# PropClue demo

PropClue is a Streamlit product demo for Indian real-estate discovery and buyer engagement. This build is a compact, runnable prototype based on the supplied brief. It uses only deterministic **synthetic demo data** for Bengaluru, Delhi, and separately tagged NCR localities.

> **Demo only.** The app does not contain verified listings, transactions, ownership, approvals, legal records, infrastructure commitments, or investment forecasts. Do not use it for legal, financial, lending, or investment decisions.

## Run locally

Use Python 3.11 or newer.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

Open <http://localhost:8501>. On first run, the app creates `data/propclue_demo.db` and seeds synthetic records. The deterministic seed includes 2 cities, 30 localities, 40 builders, 100 projects, 36 monthly market observations per locality, 8,000 synthetic sale records, 2,000 synthetic rental records, 2,000 synthetic buyers, exactly 30,000 activity events, and 300 demo leads. The first startup may take a little while while records are generated.

### Demo personas

On localhost, the start screen has **Sign in** and **Create account** modes for Buyer, Builder, and Channel partner personas. Any password is accepted and not stored; this is only for synthetic local data. On non-local hosts, open persona access is disabled by default. Set `DEMO_LOGIN_ENABLED = "true"` only if you intentionally want open demo access; never use that setting with real buyer data.

On hosted environments, **Create account** is a signup request form (no password collected). An admin reviews and approves the request, assigns any required builder scope, then the user signs in through OIDC using the exact email on the approved account. Channel partner approvals create an assigned partner workspace. The first hosted admin must be provisioned directly in the database by the deployment owner.

For authenticated access, configure Streamlit's built-in OpenID Connect support in `.streamlit/secrets.toml` or Community Cloud app secrets, then map verified emails to roles:

```toml
[auth]
redirect_uri = "https://YOUR-APP.streamlit.app/oauth2callback"
cookie_secret = "GENERATE_A_LONG_RANDOM_VALUE"
client_id = "YOUR_OIDC_CLIENT_ID"
client_secret = "YOUR_OIDC_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

```

Add the app URL to the OIDC provider's allowed redirect URLs. Each user must have a row with their verified email and approved role in `users`; builder rows must have the correct `builder_id`. Sign-in maps to that exact account and never falls back to a shared demo persona. A channel partner also needs an approved `channel_partner_profiles` row. The builder account's `builder_id` scopes its leads, projects, inventory, and campaigns.

### Persistence

Local mode uses SQLite. For a persistent hosted deployment, set `DATABASE_URL` to a PostgreSQL connection string in Streamlit Community Cloud app secrets. The app creates its tables on startup and seeds an empty database once. Without a hosted database, Streamlit Community Cloud's local filesystem can reset and demo changes may not persist across restarts.

```toml
# Add through Streamlit Cloud → App settings → Secrets (do not commit this file).
DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:5432/propclue"
```

## What works in this demo

- Local Buyer, Builder, and Channel partner demo profiles, plus verified OIDC sign-in for provisioned hosted accounts. Buyer navigation is grouped into **Market overview** (pulse + intelligence) and **Explore properties** (discover + map + compare/save + requests).
- Buyer enquiry form creates a project-scoped builder lead with a request ID, buyer-visible status, preferred follow-up channel, optional per-enquiry contact-email sharing, document requests, site-visit preference, and a 24-hour response target. The builder sees the routed request, can assign an owner, set follow-up deadlines, record contact attempts, schedule or complete visits, and mark booked or closed-lost outcomes. Both sides see the activity timeline.
- Builder lead outcome metrics separate buyer-submitted workflow requests from seeded sample leads. Response time, visits, bookings, and closed-lost counts use only workflow requests. Campaign delivery receipts remain explicitly unconnected.
- Builder inventory workspace separates Available, Reserved, and Sold sample units. Every row shows source, update time, and verification status; buyers see the same unit-level demo register on the project profile.
- Project evidence register shows source, source reference, check time, and verification status for RERA, title/EC, Khata, tax, technical/occupancy, bank approval, and price. All current rows are `Not connected` and `Unverified` until an authorized provider is integrated.
- Local demo login is host-gated. Non-local access requires configured OIDC by default; OIDC users must be provisioned as scoped user rows and mapped by exact email. Use role-based builder rows and consent records before sharing real data.
- First-time users can create a local demo account or submit a hosted access request for Buyer, Builder, or Channel partner. Admins review hosted requests, assign builder scope, and approve/reject them; passwords are not collected by this prototype.
- Channel partner workspace supports consent-confirmed referrals into the assigned builder's project pipeline. Buyers can later sign in with the referred email to track the request. Partner referrals appear in the builder's lead queue with partner attribution and activity history.
- Bengaluru, Delhi, and clearly separated NCR locality filters, with synthetic project inventory.
- Interactive PyDeck map with project focus zoom, corrected hover details, synthetic metro/IT/business/hospital/school/mall/eatery points, and illustrative locality coverage shapes.
- Project intelligence profiles, project saving, up-to-four project comparison, and CSV report downloads.
- 36-month asking-price and simulated-transaction series stored separately, rental indicators, supply, absorption, and enquiry charts.
- Buyer-first market pulse with a focused two-line price view, 12-month movement, asking-to-simulated gap, recent enquiry signals, and explicit definitions for every headline count.
- Transparent weighted locality demo score: connectivity 16%, employment access 14%, social infrastructure 13%, affordability 13%, price momentum 12%, rental demand 10%, supply-demand balance 10%, environmental quality 8%, and infrastructure certainty 4%.
- Consent-gated hover capture for buyer project/locality cards across Market pulse, Discover, Map, Market intelligence, and Compare & saved. Hover events appear in the buyer activity timeline and do not affect the buyer-intent score.
- Buyer event timeline and timestamped purpose-specific consent history.
- Buyer intent scoring with recency decay and a reason list. Scores use demo engagement events only.
- Builder portfolio filtering, consent-aware lead views, CSV export, segment filters, and simulated campaigns.
- Builder Portfolio MIS with a portfolio-project selector, daily/7/30/60/90-day simulated booking signals, engaged-lead and follow-up queues, inventory proxy, completion proxy, and current launch-stage records.
- Builder Market Intelligence combines market trends, synthetic land/development map points, and transparent pricing scenario cues. Owner/title/approval records, parcel boundaries, and actual plot availability are not connected.
- Buyer demand and campaigns share one builder workspace, with locality/project/activity segmentation and SMS/WhatsApp/voice-bot choices. Preview is simulation-only; optional live delivery sends one test message or call to a private configured destination, never to synthetic lead records.
- Persistent left-sidebar token indicator reports zero LLM calls/tokens with the current deterministic assistant.
- Buyer natural-language assistant embedded in project discovery, with synthetic QoQ/YoY and USP answers and explicit EC, Khata, title, dispute, tax, technical-clearance, and bank-document status; no paid LLM key is required.
- Discover filters for synthetic distance estimates to metro, IT parks, hospitals, malls and schools; project cards include synthetic USPs and same-locality/type/configuration price comparisons.
- Admin data coverage/licensing registry, audit records, and a guarded synthetic-data reset.
- SQLAlchemy models work with local SQLite or a PostgreSQL `DATABASE_URL`.

## Current demo boundaries

- The demo uses a compact relational model for the core walkthrough, not every table in the full product brief. It does not include PostGIS, Alembic migrations, a separate FastAPI backend, payment workflows, or a production identity/consent governance service. The local persona switcher is for synthetic data only; configure OIDC, provision scoped users, and review consent/retention policy before using real buyer data.
- Data events, enquiries, project values, localities, legal statuses, environmental signals, infrastructure points, and buyer identities are generated. Map coverage areas are illustrative shapes, not cadastral or locality boundaries.
- Buyer-to-builder enquiry and status workflows are persisted in the demo database, but no email, phone, or WhatsApp notification is sent. The builder sees contact email only after the buyer explicitly shares it; withdrawing `builder_contact` consent hides it in the pipeline.
- Inventory state changes and evidence/source records are demo entries, not synchronized feeds. No official ownership, title, approval, bank, tax, RERA, price, or availability provider is connected, and the app cannot label a project legally verified.
- Builder booking counts, locality-allocated inventory, completion percentages, and land opportunity markers are display proxies generated for the prototype; they are not operational MIS or acquisition leads. Pricing cues use synthetic locality asking/supply/enquiry signals and omit live macroeconomic inputs.
- The assistant is deterministic and answers a small set of analytical question patterns from the database. It is not a general-purpose language model.
- AI usage currently reports zero LLM tokens: intent scoring is local Python, queries use parameterized SQLAlchemy, and insights are deterministic. Flood exposure demo values are synthetic seed placeholders, not calculated from real flood observations; the locality composite uses `100 - flood placeholder` as its environmental component.
- External providers are listed for evaluation only. No external records or provider APIs are connected.

## Data provenance and procurement checklist

The in-app **Data coverage & licensing** page contains the source registry and demo substitute for each dataset.

| Priority | Dataset to evaluate | Example sources to assess | Demo substitute |
| --- | --- | --- | --- |
| Critical | State-specific transactions and market observations | Authorized registration-data providers; licensed transaction datasets | Synthetic asking and transaction-price series |
| Critical | Land records, encumbrances, title-document metadata | Authorized Karnataka/Delhi sources; Landeed or licensed legal-data providers | Synthetic due-diligence status fields |
| Critical | RERA/project approvals | Karnataka RERA, Delhi RERA, authorized providers | Synthetic project reference and document status |
| High | Project inventory, rental listings and availability | Builder feeds; licensed listing partners | Synthetic project and rent records |
| High | Building footprints and parcel data | Licensed imagery; authorized cadastral GIS; OpenStreetMap where permitted | Illustrative project points and coverage circles |
| High | Roads, transit, facilities and routing | OpenStreetMap, Google Maps Platform, Mapbox, or another licensed provider | Synthetic employment/transit markers |
| Medium–High | Land-use and environmental signals | Copernicus/Sentinel and licensed environmental data providers | Synthetic risk and locality scores |

Confirm the licence, allowed purpose, geographic scope, freshness, and redistribution terms before connecting any real data source. Do not scrape sites or bypass access controls.

## Architecture

```mermaid
flowchart LR
  U[Buyer / Builder / Admin] --> S[Streamlit UI]
  S --> A[SQLAlchemy data layer]
  A --> L[(SQLite local demo)]
  A --> P[(PostgreSQL hosted option)]
  S --> M[PyDeck map + Plotly analytics]
  S --> D[Synthetic seed + provenance registry]
```

## Data model (compact demo ER)

```mermaid
erDiagram
  BUILDERS ||--o{ USERS : has
  BUILDERS ||--o{ PROJECTS : owns_demo
  LOCALITIES ||--o{ PROJECTS : contains_demo
  LOCALITIES ||--o{ MARKET_OBSERVATIONS : tracks
  USERS ||--o{ USER_EVENTS : creates
  PROJECTS ||--o{ USER_EVENTS : relates_to
  USERS ||--o{ CONSENT_RECORDS : grants_or_withdraws
  USERS ||--o{ SAVED_PROPERTIES : saves
  PROJECTS ||--o{ SAVED_PROPERTIES : is_saved
  BUILDERS ||--o{ LEADS : receives_demo
  USERS ||--o{ LEADS : qualifies_demo
  USERS ||--o| CHANNEL_PARTNER_PROFILES : has
  USERS ||--o{ SIGNUP_REQUESTS : requests
  PROJECTS ||--o{ LEADS : relates_to
  LEADS ||--o{ LEAD_ACTIVITIES : tracks
  PROJECTS ||--o{ PROJECT_INVENTORY : lists_demo
  PROJECTS ||--o{ PROJECT_EVIDENCE : records_source_status
  BUILDERS ||--o{ CAMPAIGNS : simulates
  USERS ||--o{ AI_CONVERSATIONS : starts
  AI_CONVERSATIONS ||--o{ AI_MESSAGES : stores
  USERS ||--o{ AUDIT_LOGS : acts
```

## Deploy to Streamlit Community Cloud

Community Cloud deploys from a GitHub repository. Create or select a private repository for this demo, push this folder, connect GitHub to Community Cloud, then choose `app.py` as the entry point. Add `DATABASE_URL` under the app's Secrets settings if you want persistent cloud writes. Keep `.streamlit/secrets.toml`, database credentials, and other secrets out of Git. Configure `[auth]`, provision the initial admin and their exact verified email in the database, and review hosted signup requests from the Admin workspace. Non-local hosts disable demo persona access by default; `DEMO_LOGIN_ENABLED = "true"` is an intentional open-demo override for synthetic data only.

### Optional Twilio test delivery

The app never sends automatically. To enable the manual **Send one live test via Twilio** action, add these values under Community Cloud **Settings → Secrets** (or local `.streamlit/secrets.toml`):

```toml
TWILIO_ACCOUNT_SID = "AC…"
TWILIO_AUTH_TOKEN = "…"
TWILIO_FROM_NUMBER = "+1…" # SMS and voice sender
TWILIO_WHATSAPP_FROM = "+1…" # WhatsApp-enabled Twilio sender
CAMPAIGN_RECEIVER_PHONE = "+<E.164 test destination>"
CAMPAIGN_SEND_PIN = "<private PIN>"
```

Do not add actual credentials, recipient numbers, or the PIN to GitHub or a public config file. Each manual action sends at most one message/call to the configured test destination, applies a five-minute per-session cooldown, and does not contact the synthetic lead audience. Twilio trial accounts may also require the recipient to be verified. SMS, WhatsApp, and voice require the corresponding enabled sender in Twilio. Delivery receipts are for that one test destination only and are not buyer campaign outcome data.

Use the app's **Share** settings to control whether it is public or private. The repository and app visibility are separate settings. A private Community Cloud app requires connected GitHub access and viewer permissions. See [Streamlit's deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy), [GitHub connection instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account), and [secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

## Reset demo records

Sign in as the demo admin, open **Platform overview**, check the reset confirmation, then click **Reset demo data**. This drops and recreates only the database configured for the app. Use a dedicated demo database; do not point this control at production data.
