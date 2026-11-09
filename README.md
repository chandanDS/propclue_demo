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

The start screen offers **Enter as Buyer**, **Enter as Builder**, and **Enter as Admin**. These are role-play personas over synthetic records, not authenticated accounts.

For authenticated access, configure Streamlit's built-in OpenID Connect support in `.streamlit/secrets.toml` or Community Cloud app secrets, then map verified emails to roles:

```toml
[auth]
redirect_uri = "https://YOUR-APP.streamlit.app/oauth2callback"
cookie_secret = "GENERATE_A_LONG_RANDOM_VALUE"
client_id = "YOUR_OIDC_CLIENT_ID"
client_secret = "YOUR_OIDC_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[user_roles]
"buyer@example.com" = "buyer"
"builder@example.com" = "builder"
"admin@example.com" = "admin"
```

Add the app URL to the OIDC provider's allowed redirect URLs. The demo role switcher is deliberately not an access-control boundary.

### Persistence

Local mode uses SQLite. For a persistent hosted deployment, set `DATABASE_URL` to a PostgreSQL connection string in Streamlit Community Cloud app secrets. The app creates its tables on startup and seeds an empty database once. Without a hosted database, Streamlit Community Cloud's local filesystem can reset and demo changes may not persist across restarts.

```toml
# Add through Streamlit Cloud → App settings → Secrets (do not commit this file).
DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:5432/propclue"
```

## What works in this demo

- Role-specific buyer, builder, and admin navigation, with optional OIDC through Streamlit's built-in sign-in.
- Bengaluru, Delhi, and clearly separated NCR locality filters, with synthetic project inventory.
- Interactive PyDeck map with synthetic project points, illustrative locality coverage areas, and optional transit/employment markers.
- Project intelligence profiles, project saving, up-to-four project comparison, and CSV report downloads.
- 36-month asking-price and simulated-transaction series stored separately, rental indicators, supply, absorption, and enquiry charts.
- Buyer event timeline and timestamped purpose-specific consent history.
- Buyer intent scoring with recency decay and a reason list. Scores use demo engagement events only.
- Builder portfolio filtering, consent-aware lead views, CSV export, segment filters, and simulated campaigns. No message is sent.
- Deterministic database-backed assistant answers and persisted demo conversation history; no paid LLM key is required.
- Admin data coverage/licensing registry, audit records, and a guarded synthetic-data reset.
- SQLAlchemy models work with local SQLite or a PostgreSQL `DATABASE_URL`.

## Current demo boundaries

- The demo uses a compact relational model for the core walkthrough, not every table in the full product brief. It does not include PostGIS, Alembic migrations, a separate FastAPI backend, payment workflows, or production-grade authorization controls. The no-secret role switcher is for demo data only; configure OIDC and review role mapping before sharing beyond a trusted demo.
- Data events, enquiries, project values, localities, legal statuses, environmental signals, infrastructure points, and buyer identities are generated. Map coverage areas are illustrative shapes, not cadastral or locality boundaries.
- Builder contact, email, SMS, and WhatsApp delivery are intentionally simulated only.
- The assistant is deterministic and answers a small set of analytical question patterns from the database. It is not a general-purpose language model.
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
  PROJECTS ||--o{ LEADS : relates_to
  BUILDERS ||--o{ CAMPAIGNS : simulates
  USERS ||--o{ AI_CONVERSATIONS : starts
  AI_CONVERSATIONS ||--o{ AI_MESSAGES : stores
  USERS ||--o{ AUDIT_LOGS : acts
```

## Deploy to Streamlit Community Cloud

Community Cloud deploys from a GitHub repository. Create or select a private repository for this demo, push this folder, connect GitHub to Community Cloud, then choose `app.py` as the entry point. Add `DATABASE_URL` under the app's Secrets settings if you want persistent cloud writes. Keep `.streamlit/secrets.toml`, database credentials, and other secrets out of Git. Add `[auth]` and `[user_roles]` in Community Cloud secrets if the demo role switcher should be replaced by OIDC sign-in.

Use the app's **Share** settings to control whether it is public or private. The repository and app visibility are separate settings. A private Community Cloud app requires connected GitHub access and viewer permissions. See [Streamlit's deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy), [GitHub connection instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account), and [secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

## Reset demo records

Sign in as the demo admin, open **Platform overview**, check the reset confirmation, then click **Reset demo data**. This drops and recreates only the database configured for the app. Use a dedicated demo database; do not point this control at production data.
