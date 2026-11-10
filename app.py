from __future__ import annotations

import json
import hashlib
import html
import hmac
import os
import random
import re
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import pydeck as pdk
import requests
import streamlit as st
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError

# Streamlit Cloud secrets are exposed before database.py creates its engine.
try:
    if "DATABASE_URL" in st.secrets and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
except Exception:
    pass

from database import (
    AIConversation, AIMessage, AuditLog, Builder, Campaign, ConsentRecord, DataSource,
    ChannelPartnerProfile, Event, Lead, LeadActivity, Locality, MarketObservation,
    Project, ProjectComparison, ProjectEvidence, ProjectInventory, SavedProperty,
    SessionLocal, User, init_db, reset_demo_data, score_from_events,
)


st.set_page_config(page_title="PropClue | Real estate intelligence", page_icon="⌂", layout="wide",
                   initial_sidebar_state="expanded")


def boot_database() -> bool:
    # Re-run create_all on every Streamlit rerun so an existing local/cloud DB
    # picks up newly added tables without requiring a manual reset.
    init_db()
    return True


boot_database()


def money(value: float | int, compact: bool = False) -> str:
    value = float(value or 0)
    if compact:
        if value >= 10_000_000:
            return f"₹{value / 10_000_000:.2f} Cr"
        if value >= 100_000:
            return f"₹{value / 100_000:.1f} L"
    return f"₹{value:,.0f}"


def private_setting(name: str) -> str:
    """Read a deployment secret without putting private recipient data in source control."""
    try:
        value = st.secrets.get(name)
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    return os.environ.get(name, "").strip()


def twilio_live_config(channel: str) -> tuple[str, str, str, str, str]:
    account_sid = private_setting("TWILIO_ACCOUNT_SID")
    auth_token = private_setting("TWILIO_AUTH_TOKEN")
    recipient = private_setting("CAMPAIGN_RECEIVER_PHONE")
    if channel.startswith("WhatsApp"):
        sender = private_setting("TWILIO_WHATSAPP_FROM")
    else:
        sender = private_setting("TWILIO_FROM_NUMBER")
    send_pin = private_setting("CAMPAIGN_SEND_PIN")
    return account_sid, auth_token, sender, recipient, send_pin


def normalize_e164(number: str) -> str:
    cleaned = re.sub(r"[\s().-]", "", number)
    if not re.fullmatch(r"\+[1-9]\d{7,14}", cleaned):
        raise ValueError("The private campaign receiver setting must be an E.164 number, such as +14155552671.")
    return cleaned


def twilio_send_one(channel: str, message: str, config: tuple[str, str, str, str, str]) -> dict:
    account_sid, auth_token, sender, recipient, _send_pin = config
    if not all((account_sid, auth_token, sender, recipient)):
        raise ValueError("Twilio credentials, sender, or private recipient are not configured.")
    target = normalize_e164(recipient)
    sender_value = sender.strip()
    payload: dict[str, str]
    resource = "Messages"
    if channel.startswith("WhatsApp"):
        target = f"whatsapp:{target}"
        sender_number = normalize_e164(sender_value.removeprefix("whatsapp:"))
        sender_value = f"whatsapp:{sender_number}"
        payload = {"To": target, "From": sender_value, "Body": message}
    elif channel.startswith("Voice"):
        resource = "Calls"
        safe_text = html.escape(message[:700])
        twiml = f"<Response><Say>{safe_text}</Say><Hangup/></Response>"
        payload = {"To": target, "From": sender_value, "Twiml": twiml, "TimeLimit": "45"}
    else:
        sender_value = normalize_e164(sender_value)
        payload = {"To": target, "From": sender_value, "Body": message[:1500]}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/{resource}.json"
    response = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=(8, 25))
    try:
        response_data = response.json()
    except ValueError:
        response_data = {}
    if not response.ok:
        code = response_data.get("code")
        raise RuntimeError(f"Twilio rejected the live request (HTTP {response.status_code}{f', code {code}' if code else ''}). Check channel setup and verified sender/recipient status.")
    return {"sid": str(response_data.get("sid", "")), "status": str(response_data.get("status", "accepted"))}


def add_event(db, buyer_id: int, event_type: str, project: Project | None = None,
              locality: str | None = None, context: dict | None = None) -> None:
    loc = locality or (project.locality.name if project else "")
    city = project.city if project else (db.get(User, buyer_id).city if db.get(User, buyer_id) else "Bengaluru")
    db.add(Event(buyer_id=buyer_id, project_id=project.id if project else None, city=city, locality=loc,
                 event_type=event_type, event_at=datetime.utcnow(), session_id=st.session_state.get("demo_session", "demo-ui"),
                 source_channel="PropClue Streamlit demo", consent_status="analytics granted",
                 context_json=json.dumps(context or {})))
    db.commit()


def latest_consents(db, user_ids: list[int]) -> dict[tuple[int, str], bool]:
    if not user_ids:
        return {}
    rows = db.scalars(select(ConsentRecord).where(ConsentRecord.user_id.in_(user_ids)).order_by(ConsentRecord.recorded_at.desc(), ConsentRecord.id.desc())).all()
    result = {}
    for row in rows:
        result.setdefault((row.user_id, row.purpose), row.granted)
    return result


def get_projects(db, city: str, locality_name: str = "All localities", config: str = "All", max_budget: int | None = None):
    stmt = select(Project).where(Project.city == city)
    if locality_name != "All localities":
        stmt = stmt.join(Locality).where(Locality.name == locality_name)
    if config != "All":
        stmt = stmt.where(Project.configuration == config)
    if max_budget:
        stmt = stmt.where(Project.asking_price <= max_budget)
    return db.scalars(stmt.order_by(Project.asking_price)).all()


def project_frame(projects: list[Project]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Project": p.name, "Builder": p.builder.name, "City": p.city, "Locality": p.locality.name,
        "Configuration": p.configuration, "Area (sq ft)": p.area_sqft,
        "Asking price": p.asking_price, "Simulated transaction": p.transaction_price,
        "Rent / month": p.monthly_rent, "Rental yield %": round(p.monthly_rent * 12 / max(p.asking_price, 1) * 100, 2),
        "Status": p.status, "Project ID": p.id,
    } for p in projects])


def chart_layout(fig, title: str | None = None, height: int = 330):
    fig.update_layout(height=height, title=title, margin=dict(l=18, r=18, t=52 if title else 20, b=18),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter", color="#0C213C"), legend_title_text="",
                      hoverlabel=dict(bgcolor="#0C213C", font_color="#FFFFFF"))
    fig.update_xaxes(showgrid=False, linecolor="#DDE6ED", tickfont=dict(color="#64748B"))
    fig.update_yaxes(gridcolor="#E8EEF3", zeroline=False, tickfont=dict(color="#64748B"))
    return fig


_HOVER_CARD_COMPONENT = st.components.v2.component(
    "propclue_buyer_hover_cards",
    html="""<section class="pc-hover-root"><div class="pc-hover-title"></div><div class="pc-hover-grid"></div></section>""",
    css="""
.pc-hover-root { color: var(--st-text-color); font-family: Inter, sans-serif; }
.pc-hover-title { color: #64748B; font-size: .82rem; margin: 0 0 .5rem; }
.pc-hover-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .65rem; }
.pc-hover-card { background: var(--st-secondary-background-color); border: 1px solid var(--st-border-color); border-radius: 12px; padding: .8rem .9rem; transition: border-color .15s ease, transform .15s ease; min-height: 72px; }
.pc-hover-card:hover, .pc-hover-card:focus-visible { border-color: var(--st-primary-color); transform: translateY(-1px); outline: none; }
.pc-hover-name { font-size: .92rem; font-weight: 650; line-height: 1.25; }
.pc-hover-detail { color: #64748B; font-size: .78rem; margin-top: .35rem; }
""",
    js="""
export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const title = parentElement.querySelector(".pc-hover-title")
  const grid = parentElement.querySelector(".pc-hover-grid")
  if (!title || !grid) return
  title.textContent = data?.title || "Quick scan · hover a card to record an opted-in demo interaction"
  grid.replaceChildren()
  const timers = []
  for (const item of (data?.items || [])) {
    const card = document.createElement("div")
    card.className = "pc-hover-card"
    card.tabIndex = 0
    card.setAttribute("role", "group")
    card.setAttribute("aria-label", `${item.label || "Demo item"}. ${item.detail || "Synthetic demo record"}`)
    const name = document.createElement("div")
    name.className = "pc-hover-name"
    name.textContent = item.label || "Demo item"
    const detail = document.createElement("div")
    detail.className = "pc-hover-detail"
    detail.textContent = item.detail || "Synthetic demo record"
    card.append(name, detail)
    let timer = null
    const send = () => {
      if (!data?.tracking_allowed) return
      if (timer !== null) clearTimeout(timer)
      timer = setTimeout(() => setTriggerValue("hovered", { ...item, capture_source: "hover", capture_nonce: Date.now() }), 650)
      timers.push(timer)
    }
    card.onpointerenter = send
    card.onfocus = send
    card.onpointerleave = () => { if (timer !== null) clearTimeout(timer) }
    card.onblur = () => { if (timer !== null) clearTimeout(timer) }
    card.onclick = () => {
      if (!data?.tracking_allowed) return
      if (timer !== null) clearTimeout(timer)
      setTriggerValue("hovered", { ...item, capture_source: "click", capture_nonce: Date.now() })
    }
    card.onkeydown = (event) => {
      if ((event.key === "Enter" || event.key === " ") && data?.tracking_allowed) {
        event.preventDefault()
        if (timer !== null) clearTimeout(timer)
        setTriggerValue("hovered", { ...item, capture_source: "keyboard", capture_nonce: Date.now() })
      }
    }
    grid.append(card)
  }
  return () => timers.forEach(clearTimeout)
}
""",
)


def track_buyer_data(db, user: User, module: str, items: list[dict], title: str = "") -> None:
    """Capture intentional buyer data hovers, gated by the buyer's analytics consent."""
    if user.role != "buyer" or not items:
        return
    analytics_ok = latest_consents(db, [user.id]).get((user.id, "analytics"), False)
    if not analytics_ok:
        st.caption("Interaction capture is off for this demo profile. Sign out and enable recording on the login screen to turn it on.")
    else:
        st.caption("Recording is on: hover a card for about 0.7 seconds, focus it, or click it. A confirmation appears after capture.")
    component_key = f"propclue-hover-{user.id}-{module}"
    result = _HOVER_CARD_COMPONENT(
        key=component_key,
        data={"title": title, "items": items, "tracking_allowed": analytics_ok},
        on_hovered_change=lambda: None,
    )
    if not analytics_ok:
        return
    hovered = getattr(result, "hovered", None)
    if not isinstance(hovered, dict) or not hovered.get("id"):
        last_capture = st.session_state.get(f"last_hover_capture_{module}")
        if last_capture:
            st.success(f"Captured interaction: {last_capture['label']} · {last_capture['source']}")
        return
    dedupe_key = (module, str(hovered["id"]))
    last_events = st.session_state.setdefault("last_buyer_hover_events", {})
    now = datetime.utcnow()
    previous = last_events.get(dedupe_key)
    source = hovered.get("capture_source", "hover")
    if previous is None or (now - previous).total_seconds() >= 5:
        project = db.get(Project, int(hovered["project_id"])) if hovered.get("project_id") else None
        add_event(db, user.id, "Buyer data hovered", project,
                  locality=hovered.get("locality") or None,
                  context={"module": module, "item_type": hovered.get("item_type", "market item"),
                           "item_id": hovered["id"], "label": hovered.get("label", ""),
                           "capture_source": source, "hover_ms": 650})
        last_events[dedupe_key] = now
    st.session_state[f"last_hover_capture_{module}"] = {"label": hovered.get("label", "locality"), "source": source}


def clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def locality_score_breakdown(db, locality: Locality) -> tuple[int, list[dict]]:
    """Calculate a transparent demo score from explicitly synthetic component signals."""
    observations = db.scalars(select(MarketObservation).where(
        MarketObservation.locality_id == locality.id
    ).order_by(MarketObservation.month)).all()
    last = observations[-1] if observations else None
    prior = observations[-13] if len(observations) >= 13 else (observations[0] if observations else None)
    momentum_pct = ((last.asking_psf / prior.asking_psf) - 1) * 100 if last and prior and prior.asking_psf else 0
    affordability = clamp_score(100 * (24_000 - locality.base_rate) / 16_000)
    momentum = clamp_score(50 + momentum_pct * 4)
    rental = clamp_score(locality.rental_yield * 20)
    if observations:
        absorption = sum(x.absorption_pct for x in observations[-3:]) / min(3, len(observations))
        enquiry_units = sum(x.enquiries for x in observations[-3:]) / max(1, sum(x.available_units for x in observations[-3:]))
        absorption_score = clamp_score(absorption * 10)
        enquiry_pressure = clamp_score(enquiry_units * 180)
        balance = clamp_score(absorption_score * .65 + enquiry_pressure * .35)
    else:
        balance = 50
    stable_key = f"{locality.city}:{locality.scope}:{locality.name}".encode()
    def proxy(label: str, spread: int) -> int:
        value = int(hashlib.sha256(stable_key + label.encode()).hexdigest()[:8], 16)
        return clamp_score(locality.locality_score + value % (spread * 2 + 1) - spread)
    components = [
        {"Component": "Connectivity", "Score": int(locality.locality_score), "Weight": "16%", "Why": "Synthetic seed proxy; no measured commute or transit time."},
        {"Component": "Employment access", "Score": proxy("employment", 8), "Weight": "14%", "Why": "Deterministic synthetic proxy; no verified job/commute feed."},
        {"Component": "Social infrastructure", "Score": proxy("social", 7), "Weight": "13%", "Why": "Deterministic synthetic proxy; no verified POI coverage."},
        {"Component": "Price affordability", "Score": affordability, "Weight": "13%", "Why": "Linear scale: ₹8,000/sq ft → 100; ₹24,000/sq ft → 0, clipped to 0–100."},
        {"Component": "Price momentum", "Score": momentum, "Weight": "12%", "Why": f"50 + 4 × 12-month asking-price change ({momentum_pct:+.1f}%), clipped to 0–100."},
        {"Component": "Rental demand", "Score": rental, "Weight": "10%", "Why": f"Synthetic rental yield × 20 ({locality.rental_yield:.1f}% yield)."},
        {"Component": "Supply-demand balance", "Score": balance, "Weight": "10%", "Why": "65% latest 3-month absorption score + 35% enquiry-to-available-units pressure score."},
        {"Component": "Environmental quality", "Score": clamp_score(100 - locality.flood_score), "Weight": "8%", "Why": "100 minus the synthetic flood-exposure placeholder."},
        {"Component": "Infrastructure certainty", "Score": proxy("infrastructure", 20), "Weight": "4%", "Why": "Deterministic synthetic proxy; no project-status verification."},
    ]
    score = clamp_score(sum(row["Score"] * int(row["Weight"][:-1]) for row in components) / 100)
    return score, components


def show_locality_methodology(db, locality: Locality) -> None:
    with st.expander("How locality and flood scores work"):
        score, components = locality_score_breakdown(db, locality)
        st.markdown(f"**{locality.name}: {score}/100** · weighted sum of the component scores below.")
        st.dataframe(pd.DataFrame(components), width="stretch", hide_index=True,
                     alt=f"Synthetic component score breakdown for {locality.name}")
        st.caption("Flood exposure demo score is a synthetic scenario value assigned in the seed data. Higher means more illustrative exposure. It is not calculated from rainfall, elevation, flood maps or official records. The composite score is an educational prototype, not an investment recommendation.")


def builder_booking_history(projects: list[Project]) -> dict[int, list[date]]:
    """Deterministic illustrative booking dates; no booking system is connected."""
    today = date.today()
    result = {}
    for project in projects:
        seed = int(hashlib.sha256(f"{today.isoformat()}:{project.id}:booking-demo".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        result[project.id] = [today - timedelta(days=rng.randint(0, 89)) for _ in range(rng.randint(1, 18))]
    return result


def builder_completion(project: Project) -> int:
    if project.status.lower() == "ready to move":
        return 100
    seed = int(hashlib.sha256(f"completion:{project.id}".encode()).hexdigest()[:8], 16)
    return 18 + seed % 69


def show_builder_portfolio(db, user: User, city: str):
    builder = db.get(Builder, user.builder_id) if user.builder_id else None
    if not builder:
        st.error("This builder login is not linked to a demo builder account.")
        return
    top_header("Portfolio MIS", f"Management snapshot for {builder.name} · {city}")
    projects = db.scalars(select(Project).where(Project.builder_id == builder.id, Project.city == city).order_by(Project.name)).all()
    if not projects:
        st.info("No demo projects are assigned to this builder in the selected market.")
        return
    booking_dates = builder_booking_history(projects)
    project_labels = {f"{p.name} · {p.locality.name}": p for p in projects}
    selected_label = st.selectbox("Portfolio project", ["All portfolio projects", *project_labels], key="builder-mis-project")
    view_projects = projects if selected_label == "All portfolio projects" else [project_labels[selected_label]]
    selected_ids = [p.id for p in view_projects]
    window_cols = st.columns(5)
    for col, days, label in zip(window_cols, [1, 7, 30, 60, 90], ["Last day", "Last 7 days", "Last 30 days", "Last 60 days", "Last 90 days"]):
        total = sum(sum((date.today() - dt).days < days for dt in booking_dates[p.id]) for p in view_projects)
        col.metric(f"{label} · simulated bookings", f"{total:,}", help="Generated illustrative booking signals, not records from a booking or CRM system.", border=True)
    lead_records = builder_leads(db, builder.id, city)
    active_leads = [r for r in lead_records if r["Project record"].id in selected_ids and (datetime.utcnow() - r["Lead"].last_activity).days <= 30]
    followups = [r for r in lead_records if r["Project record"].id in selected_ids and lead_followup_due(r)]
    total_inventory = 0
    for project in view_projects:
        latest_obs = db.scalar(select(MarketObservation).where(MarketObservation.locality_id == project.locality_id).order_by(MarketObservation.month.desc()).limit(1))
        locality_project_count = max(1, db.scalar(select(func.count(Project.id)).where(Project.locality_id == project.locality_id)) or 1)
        total_inventory += int((latest_obs.available_units if latest_obs else 0) / locality_project_count)
    avg_completion = round(sum(builder_completion(p) for p in view_projects) / len(view_projects))
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Engaged leads · 30d", len(active_leads), help="Builder-authorized demo leads with synthetic activity in the last 30 days.", border=True)
    k2.metric("Follow-ups due", len(followups), help="Synthetic leads in New/Contact pending status or untouched for over 7 days.", border=True)
    k3.metric("Inventory proxy", f"{total_inventory:,}", help="Locality availability divided across demo projects; this is not builder-held stock.", border=True)
    k4.metric("Portfolio completion", f"{avg_completion}%", help="Synthetic project-status proxy; not an engineer-certified progress report.", border=True)
    st.caption("Bookings, inventory allocation, engagement and completion are illustrative synthetic values. No actual booking register, builder inventory, construction reporting or CRM is connected.")
    st.subheader("Project MIS")
    rows = []
    for p in view_projects:
        latest_obs = db.scalar(select(MarketObservation).where(MarketObservation.locality_id == p.locality_id).order_by(MarketObservation.month.desc()).limit(1))
        locality_project_count = max(1, db.scalar(select(func.count(Project.id)).where(Project.locality_id == p.locality_id)) or 1)
        rows.append({"Project": p.name, "Locality": p.locality.name, "Stage": p.status,
                     "Completion · demo": f"{builder_completion(p)}%", "Inventory · locality proxy": int((latest_obs.available_units if latest_obs else 0) / locality_project_count),
                     "Bookings · 7d demo": sum((date.today() - dt).days < 7 for dt in booking_dates[p.id]),
                     "Bookings · 30d demo": sum((date.today() - dt).days < 30 for dt in booking_dates[p.id]),
                     "Engaged leads · 30d": sum(r["Project record"].id == p.id for r in active_leads),
                     "Follow-up queue": sum(r["Project record"].id == p.id for r in followups),
                     "Possession field": p.possession})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, alt="Builder portfolio management snapshot with synthetic bookings and inventory proxies")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### Follow-up queue")
        if followups:
            st.dataframe(pd.DataFrame([{"Buyer": r["Buyer name"], "Project": r["Project"], "Status": r["Status"], "Last activity": r["Last activity"], "Next action": r["Next action"]} for r in followups[:12]]), width="stretch", hide_index=True)
        else:
            st.info("No follow-ups are due in this demo view.")
    with right:
        st.markdown("#### Launch pipeline")
        launches = [p for p in view_projects if p.status.lower() == "new launch"]
        if launches:
            launch = launches[0]
            st.write(f"**{launch.name}** · {launch.locality.name} · current project stage: {launch.status}")
            st.caption("The model has no launch calendar. This is a current New Launch stage record, not a forecast date or committed launch plan.")
        else:
            st.write("Next launch: **not available**. No project is marked New Launch here, and no launch calendar or future-phase schedule is connected.")


def pricing_guidance(db, project: Project) -> dict:
    rows = db.scalars(select(MarketObservation).where(MarketObservation.locality_id == project.locality_id).order_by(MarketObservation.month.desc()).limit(3)).all()
    latest_ask = rows[0].asking_psf if rows else project.locality.base_rate
    avg_supply = sum(x.available_units for x in rows) / len(rows) if rows else 0
    avg_demand = sum(x.enquiries for x in rows) / len(rows) if rows else 0
    project_psf = project.asking_price / max(project.area_sqft, 1)
    premium = (project_psf / max(latest_ask, 1) - 1) * 100
    demand_supply = avg_demand / max(avg_supply, 1)
    usp_count = len(project_usps(db, project))
    if demand_supply >= .35 and premium < 8 and usp_count >= 2:
        action = "Test a modest upward price scenario"
        why = "Recent synthetic enquiry activity is relatively strong against locality availability, while the project ask sits close to the locality benchmark and has multiple demo highlights."
    elif demand_supply < .12 or premium > 14:
        action = "Protect conversion; test incentives before a broad price rise"
        why = "The generated demand-to-supply signal is softer or the asking price is already materially above the locality series. Review unit mix and buyer feedback before changing list prices."
    else:
        action = "Hold pricing and review again with fresh demand data"
        why = "Current synthetic demand, supply and relative-price signals do not show a clear direction for repricing."
    return {"action": action, "why": why, "latest_ask": latest_ask, "premium": premium,
            "supply": avg_supply, "demand": avg_demand, "demand_supply": demand_supply,
            "usp_count": usp_count}


def locality_appreciation(db, locality_id: int) -> tuple[float | None, float | None, float | None]:
    rows = db.scalars(select(MarketObservation).where(
        MarketObservation.locality_id == locality_id
    ).order_by(MarketObservation.month)).all()
    if not rows:
        return None, None, None
    current = rows[-1].asking_psf
    qoq = ((current / rows[-4].asking_psf) - 1) * 100 if len(rows) >= 4 and rows[-4].asking_psf else None
    yoy = ((current / rows[-13].asking_psf) - 1) * 100 if len(rows) >= 13 and rows[-13].asking_psf else None
    return current, qoq, yoy


def project_proximity(project: Project) -> dict[str, float]:
    """Stable illustrative distances; no route or POI provider is connected."""
    result = {}
    for label, key in [("metro", "metro"), ("it_park", "IT park"), ("hospital", "hospital"),
                       ("mall", "mall"), ("school", "school"), ("business_hub", "business hub"),
                       ("eatery", "eatery")]:
        digest = hashlib.sha256(f"{project.id}:{key}".encode()).hexdigest()
        result[label] = round(.4 + (int(digest[:8], 16) % 721) / 100, 1)
    return result


def project_usps(db, project: Project) -> list[str]:
    local_score = locality_score_breakdown(db, project.locality)[0]
    yield_pct = project.monthly_rent * 12 / max(project.asking_price, 1) * 100
    amenities = json.loads(project.amenities_json or "[]")
    usps = []
    if project.status.lower() in {"ready to move", "ready"}:
        usps.append("Ready-to-move scenario")
    if local_score >= 80:
        usps.append("Higher synthetic locality score")
    if yield_pct >= 3.8:
        usps.append("Higher demo rental yield")
    if amenities:
        usps.append(f"{len(amenities)} listed demo amenities")
    if project.possession:
        usps.append(f"Possession: {project.possession} (demo)")
    return usps[:3] or ["Synthetic project comparison record"]


def legal_evidence_status(project: Project) -> dict[str, str]:
    return {
        "Project": project.name,
        "Title deed / chain": "Not connected",
        "Encumbrance certificate (EC)": "Not connected",
        "Khata / property record": "Not connected",
        "RERA / approvals": "Not connected",
        "Property tax receipts": "Not connected",
        "Technical / occupancy clearance": "Not connected",
        "Bank approval / letter": "Not verified or attached",
    }


def current_user(db):
    user_id = st.session_state.get("user_id")
    return db.get(User, user_id) if user_id else None


def login_screen(db):
    brand_lockup()
    st.markdown("<h1 style='font-family:Inter, sans-serif;font-weight:800;color:#0C213C;line-height:1.08'>Find your next property with more clarity.</h1>", unsafe_allow_html=True)
    st.caption("Buyer and builder demo access · generated data only")
    left, right = st.columns([1.0, 1.0], gap="large")
    with left:
        st.markdown("### One place to research, compare and ask")
        st.write("Explore locality price trends, project highlights, synthetic proximity estimates, map points and a property Q&A assistant.")
        st.markdown("**Buyer workspace**")
        st.write("Market overview · Explore properties · Compare and save · Ask PropClue")
        st.caption("Legal, tax, title, bank, price, distance, amenity and availability details are generated or unverified demo data. No property is confirmed dispute-free.")
    with right:
        with st.container(border=True):
            st.subheader("Sign in or create a demo account")
            st.caption("Demo mode · any password is accepted and never stored. Use only synthetic data.")
            mode = st.segmented_control("Account access", ["Sign in", "Create account"], default="Sign in", key="login-mode")
            account_type = st.selectbox("Account type", ["Buyer", "Builder", "Channel partner"], key="login_account_type")
            city = st.selectbox("Market", ["Bengaluru", "Delhi"], key="login_market")
            builders = db.scalars(select(Builder).order_by(Builder.name)).all()
            builder_by_name = {f"{x.name} · {x.city}": x for x in builders}
            with st.form("demo_login_form", border=False):
                username = st.text_input("Username or email", placeholder="Enter a demo email")
                _password = st.text_input("Password", type="password", placeholder="Demo only; not validated")
                selected_builder = None
                if account_type in {"Builder", "Channel partner"} and builder_by_name:
                    eligible_builders = {label: builder for label, builder in builder_by_name.items() if builder.city == city}
                    selected_builder = st.selectbox("Builder company / requested partner affiliation", list(eligible_builders))
                partner_company = st.text_input("Channel partner company", max_chars=120) if account_type == "Channel partner" else ""
                record_interactions = st.checkbox("Record my demo interactions in this session", value=True,
                                                  help="Hover and explore actions are stored as synthetic demo activity.")
                action_label = "Create demo account" if mode == "Create account" else "Continue to demo"
                submit = st.form_submit_button(action_label,
                                               type="primary", width="stretch")
            if submit:
                raw_name = username.strip()
                if not raw_name:
                    st.error("Enter a username to create or open a demo profile.")
                    st.stop()
                if "@" in raw_name:
                    email = raw_name.lower()
                    display_name = raw_name.split("@", 1)[0]
                else:
                    slug = re.sub(r"[^a-z0-9]+", ".", raw_name.lower()).strip(".") or "demo.user"
                    email = f"{slug}@propclue.test"
                    display_name = raw_name[:100]
                role = account_type.lower()
                role = {"channel partner": "channel_partner"}.get(role, role)
                user = db.scalar(select(User).where(User.email == email))
                if mode == "Create account" and user:
                    st.error("An account with this email already exists. Choose Sign in or use another email.")
                    st.stop()
                if user and user.role != role:
                    st.error("That username already has a different demo role. Use another username or sign in with its saved role.")
                    st.stop()
                selected_builder_row = builder_by_name.get(selected_builder) if selected_builder else None
                builder_id = selected_builder_row.id if role == "builder" and selected_builder_row else None
                if user:
                    user.name = display_name
                    user.city = city
                    user.role = role
                    user.builder_id = builder_id
                else:
                    user = User(role=role, name=display_name, email=email, city=city,
                                locality="Whitefield" if city == "Bengaluru" else "Dwarka", builder_id=builder_id)
                    db.add(user)
                    db.flush()
                db.add(ConsentRecord(user_id=user.id, purpose="analytics", granted=record_interactions))
                if role == "channel_partner":
                    profile = db.scalar(select(ChannelPartnerProfile).where(ChannelPartnerProfile.user_id == user.id))
                    if not profile:
                        profile = ChannelPartnerProfile(user_id=user.id)
                        db.add(profile)
                    profile.company_name = partner_company.strip() or display_name
                    profile.builder_id = selected_builder_row.id if selected_builder_row else None
                    profile.status = "Approved · demo"
                    profile.approved_at = datetime.utcnow()
                db.add(AuditLog(actor_id=user.id, action="demo_login", detail=json.dumps({"role": role, "city": city})))
                try:
                    db.commit()
                except SQLAlchemyError as exc:
                    db.rollback()
                    st.error("Could not save the demo account. Refresh the app and try another email; if it continues, check the database connection.")
                    return
                st.session_state.user_id = user.id
                st.session_state.city_filter = city
                st.session_state.demo_session = str(uuid.uuid4())
                st.session_state.auth_mode = "demo"
                st.rerun()


def brand_lockup(compact: bool = False):
    size = 40 if compact else 54
    icon, name = st.columns([.14, .86], vertical_alignment="center")
    with icon:
        st.image("assets/propclue-logo.png", width=size)
    with name:
        font_size = "1.25rem" if compact else "1.6rem"
        word_color = "#F5F8FC" if compact else "#0C213C"
        accent = "#5FE3B3" if compact else "#0D9488"
        tagline_size = ".76rem" if compact else ".86rem"
        st.markdown(f"<div style='line-height:1.15'><b style='font-size:{font_size};letter-spacing:.08em;color:{word_color}'>PROPCLUE</b><br><small style='font-size:{tagline_size};letter-spacing:.12em;color:{accent};font-weight:800'>REAL ESTATE INTELLIGENCE</small></div>", unsafe_allow_html=True)


def top_header(title: str, subtitle: str = ""):
    brand_lockup()
    st.title(title, anchor=False)
    if subtitle:
        st.caption(subtitle)
    st.caption("Demo workspace · all generated signals are synthetic and unverified")


def get_city_filter(db, user: User) -> str:
    if user.role == "builder" and user.builder_id:
        builder = db.get(Builder, user.builder_id)
        if builder:
            st.caption(f"Portfolio market · {builder.city}")
            st.session_state.city_filter = builder.city
            return builder.city
    choices = ["Bengaluru", "Delhi"]
    default = st.session_state.get("city_filter", user.city if user.city in choices else choices[0])
    selected = st.selectbox("Market", choices, index=choices.index(default) if default in choices else 0)
    st.session_state.city_filter = selected
    return selected


def show_overview(db, user: User, city: str, builder_id: int | None = None, show_header: bool = True):
    if show_header:
        top_header("Market pulse", f"A clearer view of prices, buyer interest and available inventory in {city}.")
    projects = db.scalars(select(Project).where(Project.city == city)).all()
    if builder_id:
        projects = [p for p in projects if p.builder_id == builder_id]
    localities = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    loc_count = len(localities)
    enquiries = db.scalar(select(func.count(Event.id)).where(
        Event.city == city, Event.event_type == "Enquiry submitted")) or 0
    leads = db.scalar(select(func.count(Lead.id)).where(Lead.builder_id == builder_id)) if builder_id else None
    obs = db.scalars(select(MarketObservation).join(Locality).where(
        Locality.city == city).order_by(MarketObservation.month)).all()
    locality_names = {loc.id: loc.name for loc in localities}
    obs_df = pd.DataFrame([{"Month": x.month, "Locality": locality_names[x.locality_id],
                            "Asking price": x.asking_psf, "Simulated transaction": x.transaction_psf,
                            "Available units": x.available_units, "Absorption %": x.absorption_pct,
                            "Buyer enquiries": x.enquiries} for x in obs])
    latest = obs_df.sort_values("Month").groupby("Locality", as_index=False).tail(1) if not obs_df.empty else obs_df
    inventory = int(latest["Available units"].sum()) if not latest.empty else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Projects in view", f"{len(projects):,}", help="Number of synthetic project records in this city. A project can contain many homes.", border=True)
    c2.metric("Locality markets", f"{loc_count:,}", help="Synthetic locality records for this city. Delhi includes separately tagged NCR localities.", border=True)
    c3.metric("Recorded demo enquiries", f"{enquiries:,}", help="Lifetime count of synthetic ‘Enquiry submitted’ events stored for this city, including seed events and demo actions. Not unique buyers or real inquiries.", border=True)
    c4.metric("Portfolio leads" if builder_id else "Available inventory",
              f"{leads:,}" if builder_id and leads is not None else f"{inventory:,}",
              help="Latest synthetic monthly available-unit estimate summed across locality markets. It is not a live listing count.", border=True)
    if not projects:
        st.info("No projects in this market yet.")
        return
    focus_options = ["City average"] + [x.name for x in localities]
    focus = st.selectbox("Price trend focus", focus_options, key="pulse_focus")
    focus_df = obs_df if focus == "City average" else obs_df[obs_df["Locality"] == focus]
    if focus == "City average":
        trend_base = focus_df.groupby("Month", as_index=False).agg({
            "Asking price": "mean", "Simulated transaction": "mean",
            "Available units": "sum", "Absorption %": "mean", "Buyer enquiries": "sum"})
        focus_label = f"{city} locality average"
    else:
        trend_base = focus_df.sort_values("Month").copy()
        focus_label = focus
    if not trend_base.empty:
        current = trend_base.iloc[-1]
        previous = trend_base.iloc[-13] if len(trend_base) >= 13 else trend_base.iloc[0]
        asking_change = (current["Asking price"] / previous["Asking price"] - 1) * 100 if previous["Asking price"] else 0
        transaction_gap = (current["Asking price"] - current["Simulated transaction"]) / current["Asking price"] * 100 if current["Asking price"] else 0
        price_long = trend_base.melt(id_vars="Month", value_vars=["Asking price", "Simulated transaction"],
                                     var_name="Price signal", value_name="₹ per sq ft")
        with st.container(border=True):
            st.subheader(f"Price pulse · {focus_label}")
            st.caption("One focus at a time keeps the asking and simulated transaction lines easy to compare.")
            fig = px.line(price_long, x="Month", y="₹ per sq ft", color="Price signal",
                          color_discrete_map={"Asking price": "#0D9488", "Simulated transaction": "#3B82F6"})
            fig.update_traces(line=dict(width=3), mode="lines+markers", marker=dict(size=4),
                              hovertemplate="%{x|%b %Y}<br>₹%{y:,.0f} / sq ft<extra>%{fullData.name}</extra>")
            fig.update_layout(legend=dict(orientation="h", y=1.14, x=0), yaxis_title="Price · ₹ / sq ft",
                              xaxis_title="", title=None, hovermode="x unified")
            st.plotly_chart(chart_layout(fig, height=390), width="stretch",
                            alt=f"Asking and simulated transaction price series for {focus_label}",
                            config={"displayModeBar": False})
            st.caption(f"Monthly synthetic data · latest observation {current['Month']:%b %Y}. Simulated transaction is a generated comparison signal, not a verified sale record or quote.")
        k1, k2, k3 = st.columns(3)
        k1.metric("12-month asking movement", f"{asking_change:+.1f}%", help="Change from the synthetic observation 12 months earlier to the latest month.", border=True)
        k2.metric("Asking vs simulated gap", f"{transaction_gap:.1f}%", help="Difference between generated asking and simulated transaction series in the latest observation; not a negotiated discount.", border=True)
        k3.metric("Latest monthly enquiry signals", f"{int(current['Buyer enquiries']):,}", help="Generated enquiries in the latest monthly observation; distinct from the lifetime event count above.", border=True)

    if not obs_df.empty:
        recent = obs_df.sort_values("Month").groupby("Locality", as_index=False).tail(3)
        demand = recent.groupby("Locality", as_index=False)["Buyer enquiries"].sum().sort_values("Buyer enquiries", ascending=False).head(6)
        most_active = demand.iloc[0]
        st.subheader("What the demo signals suggest")
        insight_col, chart_col = st.columns([.85, 1.15], gap="large")
        with insight_col:
            with st.container(border=True):
                st.markdown("**Buyer attention**")
                st.write(f"{most_active['Locality']} has the strongest synthetic enquiry activity in the latest three observations ({int(most_active['Buyer enquiries']):,} signals).")
                st.caption("Treat this as a prompt to compare projects and availability. It is not a forecast of demand.")
            with st.container(border=True):
                st.markdown("**Reading the price lines**")
                st.write("Asking price is the generated advertised-price series. Simulated transaction price is a separate, generated comparison series intended to illustrate how achieved prices might differ.")
                st.caption("No actual buyer-seller transactions are included in this demo.")
        with chart_col:
            fig = px.bar(demand.sort_values("Buyer enquiries"), x="Buyer enquiries", y="Locality", orientation="h",
                         color_discrete_sequence=["#0D9488"])
            fig.update_layout(title="Recent buyer attention · last 3 observations", xaxis_title="Synthetic enquiry signals", yaxis_title="", showlegend=False)
            st.plotly_chart(chart_layout(fig, height=280), width="stretch", alt="Synthetic enquiry activity by locality over the latest three months", config={"displayModeBar": False})

    scored = [(loc, *locality_score_breakdown(db, loc)) for loc in localities]
    hot = sorted(scored, key=lambda row: row[1], reverse=True)[:5]
    st.subheader("Locality snapshot")
    snap = pd.DataFrame([{"Scope": loc.scope, "Locality": loc.name, "Demo score": score,
                          "Indicative asking · ₹/sq ft": int(loc.base_rate), "Demo rental yield": f"{loc.rental_yield:.1f}%",
                          "Flood exposure demo": loc.flood_score} for loc, score, _ in hot])
    st.dataframe(snap, width="stretch", hide_index=True, alt="Top synthetic locality scores, asking prices, rental yields and flood placeholders")
    hover_items = [{"id": f"locality-{loc.id}", "item_type": "locality", "label": loc.name,
                    "detail": f"{score}/100 demo score · flood placeholder {loc.flood_score}/100",
                    "locality": loc.name} for loc, score, _ in hot]
    track_buyer_data(db, user, "market-pulse", hover_items, "Quick scan · hover a locality card to record an opted-in interaction")
    if hot:
        show_locality_methodology(db, hot[0][0])


def show_discover(db, user: User, city: str, show_header: bool = True):
    if show_header:
        top_header("Discover projects", "Find a project, compare its synthetic appreciation and inspect the demo evidence checklist.")
    city_localities = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    f1, f2, f3, f4 = st.columns([1.1, 1, 1, 1.25])
    locality = f1.selectbox("Locality", ["All localities"] + [x.name for x in city_localities], key="discover_locality")
    property_type = f2.selectbox("Property type", ["All", "Apartment", "Villa", "Plot"], key="discover_property_type")
    config = f3.selectbox("Configuration", ["All", "1 BHK", "2 BHK", "3 BHK", "4 BHK"], key="discover_configuration")
    max_budget_cr = f4.slider("Max asking price (₹ Cr)", 0.5, 6.0, 3.0, 0.1, key="discover_budget")
    with st.expander("Distance filters · synthetic straight-line estimates"):
        d1, d2, d3 = st.columns(3)
        max_metro = d1.slider("Metro (demo km)", .5, 8.0, 8.0, .5, key="filter_metro")
        max_it = d2.slider("IT park (demo km)", .5, 8.0, 8.0, .5, key="filter_itpark")
        max_hospital = d3.slider("Hospital (demo km)", .5, 8.0, 8.0, .5, key="filter_hospital")
        d4, d5 = st.columns(2)
        max_mall = d4.slider("Mall (demo km)", .5, 8.0, 8.0, .5, key="filter_mall")
        max_school = d5.slider("School (demo km)", .5, 8.0, 8.0, .5, key="filter_school")
    projects = get_projects(db, city, locality, config, int(max_budget_cr * 10_000_000))
    if property_type != "All":
        projects = [p for p in projects if p.property_type == property_type]
    distance_limits = {"metro": max_metro, "it_park": max_it, "hospital": max_hospital,
                       "mall": max_mall, "school": max_school}
    projects = [p for p in projects if all(project_proximity(p)[key] <= limit for key, limit in distance_limits.items())]
    all_city_projects = db.scalars(select(Project).where(Project.city == city)).all()
    similar_totals: dict[tuple[int, str, str], list[float]] = {}
    for item in all_city_projects:
        group_key = (item.locality_id, item.property_type, item.configuration)
        similar_totals.setdefault(group_key, []).append(item.asking_price / max(item.area_sqft, 1))
    rows = []
    for p in projects:
        current_psf, qoq, yoy = locality_appreciation(db, p.locality_id)
        nearby = project_proximity(p)
        comparable = similar_totals.get((p.locality_id, p.property_type, p.configuration), [])
        rows.append({"Project": p.name, "Locality": p.locality.name, "Type": p.property_type,
                     "Configuration": p.configuration, "Asking price": p.asking_price,
                     "Project ₹/sq ft": round(p.asking_price / max(p.area_sqft, 1)),
                     "Similar project avg ₹/sq ft": round(sum(comparable) / len(comparable)) if comparable else None,
                     "Locality QoQ": qoq, "Locality YoY": yoy,
                     "Metro demo km": nearby["metro"], "IT park demo km": nearby["it_park"],
                     "Hospital demo km": nearby["hospital"], "Status": p.status, "Project ID": p.id})
    frame = pd.DataFrame(rows)
    st.caption(f"Showing {len(projects)} synthetic projects · price changes compare locality-average asking ₹/sq ft · no postal-code feed is connected.")
    if frame.empty:
        st.warning("No projects match those filters. Try a higher budget or another locality.")
    else:
        panel, chart = st.columns([1.3, .7], gap="large")
        with panel:
            visible = frame[["Project", "Locality", "Type", "Configuration", "Project ₹/sq ft",
                             "Similar project avg ₹/sq ft", "Locality QoQ", "Locality YoY", "Metro demo km"]].copy()
            st.dataframe(visible, width="stretch", hide_index=True, height=380,
                         column_config={
                             "Project ₹/sq ft": st.column_config.NumberColumn(format="₹%,.0f"),
                             "Similar project avg ₹/sq ft": st.column_config.NumberColumn(format="₹%,.0f"),
                             "Locality QoQ": st.column_config.NumberColumn(format="%.1f%%"),
                             "Locality YoY": st.column_config.NumberColumn(format="%.1f%%"),
                             "Metro demo km": st.column_config.NumberColumn(format="%.1f km"),
                         }, alt="Filtered synthetic projects with locality appreciation and illustrative proximity")
            hover_items = [{"id": f"project-{p.id}", "item_type": "project", "project_id": p.id,
                            "label": p.name, "detail": f"{p.locality.name} · {p.configuration} · {money(p.asking_price, True)} asking",
                            "locality": p.locality.name} for p in projects[:8]]
            track_buyer_data(db, user, "discover", hover_items, "Quick scan · hover a project card to record an opted-in interaction")
            choices = {f"{p.name} · {p.locality.name} · {money(p.asking_price, True)}": p.id for p in projects}
            selected_label = st.selectbox("Open project details", list(choices), key="discover_project_profile")
            selected = db.get(Project, choices[selected_label])
            if st.button("View project details", type="primary", width="stretch", key="open-discover-project"):
                st.session_state.selected_project = selected.id
                add_event(db, user.id, "Project viewed", selected, context={"configuration": selected.configuration, "budget_max": int(max_budget_cr * 10_000_000)})
                st.rerun()
        with chart:
            locality_chart = frame.groupby("Locality", as_index=False).agg({"Project ₹/sq ft": "median", "Project": "count"})
            locality_chart = locality_chart.sort_values("Project ₹/sq ft", ascending=True)
            fig = px.bar(locality_chart, x="Project ₹/sq ft", y="Locality", orientation="h",
                         color_discrete_sequence=["#0D9488"], hover_data={"Project": True})
            fig.update_layout(title="Median asking price · ₹/sq ft", xaxis_title="Median project asking price per sq ft", yaxis_title="")
            st.plotly_chart(chart_layout(fig, height=max(330, 31 * len(locality_chart))), width="stretch",
                            alt="Median synthetic project asking price per square foot by locality",
                            config={"displayModeBar": False})
    profile_id = st.session_state.get("selected_project")
    profile = db.get(Project, profile_id) if profile_id else None
    if profile and profile.city == city:
        show_project_profile(db, user, profile)


def show_project_profile(db, user: User, project: Project):
    loc = project.locality
    loc_score, _ = locality_score_breakdown(db, loc)
    current_psf, qoq, yoy = locality_appreciation(db, loc.id)
    proximity = project_proximity(project)
    st.subheader(project.name)
    st.caption(f"{project.builder.name} · {project.city} / {loc.scope} · {loc.name} · {project.status} · Project ID {project.id}")
    with st.container(border=True):
        a, b, c, d = st.columns(4)
        a.metric("Indicative asking", money(project.asking_price, True), border=True)
        b.metric("Simulated transaction", money(project.transaction_price, True), help="Synthetic comparison value, not a verified sale or quote.", border=True)
        c.metric("Area", f"{project.area_sqft:,} sq ft", border=True)
        d.metric("Demo monthly rent", money(project.monthly_rent), border=True)
        st.caption("Asking and simulated transaction values are distinct synthetic signals; neither is a verified market quote.")
        p1, p2, p3 = st.columns(3)
        p1.write(f"**Configuration:** {project.configuration}")
        p2.write(f"**Building:** {project.tower_count} towers · {project.floors} floors")
        p3.write(f"**Possession:** {project.possession} (synthetic)")
        st.write("**Amenities:** " + " · ".join(json.loads(project.amenities_json)))
        st.markdown("**Property USPs · synthetic demo**")
        for usp in project_usps(db, project):
            st.badge(usp, color="green")
        st.caption(f"Nearby distances · synthetic straight-line estimates only: metro {proximity['metro']:.1f} km · IT park {proximity['it_park']:.1f} km · hospital {proximity['hospital']:.1f} km · school {proximity['school']:.1f} km · mall {proximity['mall']:.1f} km.")
        trend_cols = st.columns(3)
        trend_cols[0].metric("Latest locality asking", money(current_psf) + "/sq ft" if current_psf else "Unavailable", help="Latest synthetic locality-average asking price per square foot.", border=True)
        trend_cols[1].metric("Locality QoQ", f"{qoq:+.1f}%" if qoq is not None else "Unavailable", help="Quarter-over-quarter change in the synthetic locality-average asking series.", border=True)
        trend_cols[2].metric("Locality YoY", f"{yoy:+.1f}%" if yoy is not None else "Unavailable", help="Year-over-year change in the synthetic locality-average asking series. No postal-code data is connected.", border=True)
        st.write(f"**Locality intelligence demo score:** {loc_score}/100 · **Rental yield demo:** {loc.rental_yield:.1f}% · **Flood exposure demo score:** {loc.flood_score}/100")
        show_locality_methodology(db, loc)
        inventory_rows = db.scalars(select(ProjectInventory).where(ProjectInventory.project_id == project.id).order_by(ProjectInventory.unit_label)).all()
        with st.container(border=True):
            st.subheader("Unit availability")
            if inventory_rows:
                st.dataframe(pd.DataFrame([{"Unit / sample": row.unit_label, "Availability": row.availability,
                    "Indicative ask": money(row.asking_price, True), "Source": row.source_name,
                    "Updated": row.updated_at.strftime("%d %b %Y %H:%M UTC"), "Verification": row.verification_status}
                    for row in inventory_rows]), width="stretch", hide_index=True, alt=f"Unit availability and source status for {project.name}")
                st.caption("These are synthetic sample units. They do not establish current builder-held stock or a live reservation.")
            else:
                st.info("No unit inventory feed is connected for this project.")
        with st.container(border=True):
            st.subheader("Legal, technical and bank checks")
            evidence_rows = db.scalars(select(ProjectEvidence).where(ProjectEvidence.project_id == project.id).order_by(ProjectEvidence.evidence_type)).all()
            st.info("No official documents or provider records are connected. This demo cannot confirm that a property is dispute-free.")
            legal_cols = st.columns(3)
            legal_cols[0].metric("Legal / title score", "Not assessed", help="No underlying verified documents are available to calculate a legal score.", border=True)
            legal_cols[1].metric("Verified documents", "0 / 7", help="No originals or official records are linked to this synthetic project.", border=True)
            legal_cols[2].metric("Bank approval", "Not verified", help="No bank approval records or approval letter are attached.", border=True)
            evidence_frame = pd.DataFrame([{"Check": row.evidence_type, "Status": row.status, "Source": row.source_name,
                "Reference": row.source_reference or "—", "Last checked": row.checked_at.strftime("%d %b %Y %H:%M UTC") if row.checked_at else "Never",
                "Verification": row.verification_status} for row in evidence_rows])
            if evidence_frame.empty:
                evidence_frame = pd.DataFrame([{"Check": key, "Status": value, "Source": "No connected provider", "Reference": "—", "Last checked": "Never", "Verification": "Unverified"} for key, value in legal_evidence_status(project).items() if key != "Project"])
            st.dataframe(evidence_frame,
                         width="stretch", hide_index=True, alt=f"Unverified legal, tax, technical, and bank document checklist for {project.name}")
            st.caption("This source register supports provenance and freshness. ‘Not connected’ is not a negative legal finding. Do not use these demo records to make a purchase or lending decision.")
        st.caption(f"Data provenance: Synthetic demo data · generated {date.today().isoformat()} · unverified · confidence: illustrative only")
        b1, b2 = st.columns(2)
        if b1.button("Save project", key=f"save-{project.id}"):
            exists = db.scalar(select(SavedProperty).where(SavedProperty.user_id == user.id, SavedProperty.project_id == project.id))
            if not exists:
                db.add(SavedProperty(user_id=user.id, project_id=project.id))
            add_event(db, user.id, "Project shortlisted", project)
            st.success("Saved to your demo shortlist.")
        report = pd.DataFrame([{"data_label": "SYNTHETIC DEMO — NOT VERIFIED", "project": project.name,
                                "builder": project.builder.name, "city": project.city, "locality": loc.name,
                                "configuration": project.configuration, "asking_price_inr": project.asking_price,
                                "simulated_transaction_inr": project.transaction_price, "area_sqft": project.area_sqft,
                                "monthly_rent_inr": project.monthly_rent, "legal_status": project.legal_status}])
        b2.download_button("Download demo report CSV", report.to_csv(index=False), file_name=f"propclue-project-{project.id}-demo.csv", mime="text/csv", on_click=lambda: add_event(db, user.id, "Report downloaded", project))
        st.markdown("#### Contact the builder / request a visit")
        st.caption("Submitting creates a buyer-visible request and routes a lead to this project’s builder. Demo requests are stored in this app only; no email, phone, or WhatsApp is sent.")
        with st.form(f"buyer-enquiry-{project.id}", clear_on_submit=True):
            contact_channel = st.selectbox("Preferred follow-up", ["Email", "In-app only"], key=f"buyer-channel-{project.id}")
            share_contact = st.checkbox("Allow this builder to see my account email for this enquiry", value=False, key=f"share-contact-{project.id}")
            visit_requested = st.checkbox("Request a site visit", key=f"visit-request-{project.id}")
            visit_date = st.date_input("Preferred visit date", value=date.today() + timedelta(days=3), key=f"visit-date-{project.id}")
            visit_time = st.time_input("Preferred visit time", value=datetime.strptime("11:00", "%H:%M").time(), key=f"visit-time-{project.id}")
            requested_documents = st.multiselect("Request documents", ["RERA registration", "Title chain / EC", "Khata / property record", "Property tax", "Technical / occupancy clearance", "Bank approval letter"], key=f"documents-{project.id}")
            buyer_note = st.text_area("Note for the builder", max_chars=500, key=f"buyer-note-{project.id}")
            submit_enquiry = st.form_submit_button("Send enquiry to builder", type="primary")
        if submit_enquiry:
            if contact_channel == "Email" and not share_contact:
                st.error("To receive follow-up by email, explicitly allow the builder to see your account email, or choose In-app only.")
                return
            now = datetime.utcnow()
            lead = Lead(builder_id=project.builder_id, buyer_id=user.id, project_id=project.id,
                        status="New", intent_score=0, intent_category="Low", last_activity=now,
                        assigned_to="Unassigned")
            db.add(lead)
            db.flush()
            requested_visit = datetime.combine(visit_date, visit_time) if visit_requested else None
            db.add(LeadActivity(lead_id=lead.id, actor_id=user.id, action="Buyer enquiry submitted",
                                status_after="New", note=buyer_note.strip(), contact_channel=contact_channel,
                                share_contact=share_contact, requested_documents_json=json.dumps(requested_documents),
                                due_at=now + timedelta(hours=24), appointment_at=requested_visit, created_at=now))
            if share_contact:
                db.add(ConsentRecord(user_id=user.id, purpose="builder_contact", granted=True,
                                     recorded_at=now, source="Project enquiry form"))
            db.add(AuditLog(actor_id=user.id, action="buyer_enquiry_submitted",
                            detail=json.dumps({"lead_id": lead.id, "project_id": project.id, "contact_shared": share_contact})))
            add_event(db, user.id, "Enquiry submitted", project, context={"lead_id": lead.id})
            st.success(f"Your request was sent to {project.builder.name}. Request ID: PC-{lead.id:06d}. Check My requests for progress.")
        with st.expander("My requests for this project"):
            buyer_leads = db.scalars(select(Lead).where(Lead.buyer_id == user.id, Lead.project_id == project.id).order_by(Lead.last_activity.desc())).all()
            if not buyer_leads:
                st.info("You have not contacted this builder about this project yet.")
            for buyer_lead in buyer_leads:
                st.write(f"**PC-{buyer_lead.id:06d} · {buyer_lead.status}** · assigned to {buyer_lead.assigned_to} · updated {buyer_lead.last_activity.strftime('%d %b %Y %H:%M UTC')}")
                activities = db.scalars(select(LeadActivity).where(LeadActivity.lead_id == buyer_lead.id).order_by(LeadActivity.created_at)).all()
                for activity in activities:
                    st.caption(f"{activity.created_at.strftime('%d %b %Y %H:%M UTC')} · {activity.action} · {activity.status_after}" + (f" · visit {activity.appointment_at.strftime('%d %b %Y %H:%M')}" if activity.appointment_at else ""))
        obs = db.scalars(select(MarketObservation).where(MarketObservation.locality_id == loc.id).order_by(MarketObservation.month)).all()
        series = pd.DataFrame([{"Month": x.month, "Asking price": x.asking_psf, "Simulated transaction": x.transaction_psf} for x in obs])
        fig = px.line(series, x="Month", y=["Asking price", "Simulated transaction"], title="36-month locality series · ₹ per sq ft")
        st.plotly_chart(chart_layout(fig, height=300), width="stretch", alt="36-month asking and simulated transaction price trend for this locality", config={"displayModeBar": False})


def show_map(db, user: User, city: str, show_header: bool = True):
    if show_header:
        top_header("Explore properties · Map", "Inspect synthetic project points, nearby highlights and demo evidence status.")
    locs = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    projects = db.scalars(select(Project).where(Project.city == city).order_by(Project.name)).all()
    if not locs:
        st.info("No localities are available in this demo market.")
        return
    selected_loc = st.selectbox("Focus locality", [x.name for x in locs], key="map-locality-focus")
    selected = next((x for x in locs if x.name == selected_loc), locs[0])
    locality_projects = [p for p in projects if p.locality_id == selected.id]
    choices = {"All projects in this locality": None}
    choices.update({f"{p.name} · {p.configuration}": p.id for p in locality_projects})
    chosen_label = st.selectbox("Project / building focus", list(choices), key="map-building-focus")
    selected_project = next((p for p in locality_projects if p.id == choices[chosen_label]), None) if choices[chosen_label] else None
    poi_types = st.multiselect("Nearby demo points", ["Metro", "IT park", "Business hub", "Hospital", "School", "Mall", "Eatery"],
                               default=["Metro", "IT park", "Hospital", "School"], key="map-poi-types")
    ring_controls = st.columns(2)
    show_areas = ring_controls[0].toggle("Locality circles · 1 km guide", value=True, key="map-coverage-toggle")
    show_project_rings = ring_controls[1].toggle("Project circles · 250 m guide", value=True, key="map-project-circle-toggle")
    st.caption("Employment points are illustrative IT parks/business hubs; transit points are illustrative metro nodes. All pins and distance estimates are synthetic straight-line examples. Locality circles and project rings are visual guides, not boundaries or measured catchments.")
    poi_offsets = {"Metro": (-.009, -.006), "IT park": (.010, .008), "Business hub": (.006, -.010),
                   "Hospital": (-.006, .010), "School": (.012, -.002), "Mall": (-.012, .003), "Eatery": (.003, .013)}
    poi_data = []
    for i, kind in enumerate(poi_types):
        dx, dy = poi_offsets[kind]
        poi_data.append({"name": f"Demo {kind}", "locality": selected.name, "detail": "Illustrative nearby point",
                         "status": "Synthetic · not verified", "price": "", "kind": kind,
                         "coordinates": [selected.longitude + dx, selected.latitude + dy]})
    project_data = []
    for p in locality_projects:
        # Small deterministic offsets keep nearby project markers individually selectable.
        h = int(hashlib.sha256(str(p.id).encode()).hexdigest()[:8], 16)
        lat = p.latitude + ((h % 101) - 50) / 12000
        lon = p.longitude + (((h // 101) % 101) - 50) / 12000
        if selected_project and p.id != selected_project.id:
            continue
        usps = ", ".join(project_usps(db, p))
        project_data.append({"name": p.name, "locality": p.locality.name,
            "detail": f"{p.configuration} · {p.area_sqft:,} sq ft · USP: {usps}",
            "status": "Legal/title/technical: not connected · bank: not verified · tax: not connected",
            "price": f"Asking {money(p.asking_price, True)} · synthetic point",
            "coordinates": [lon, lat], "kind": "Project", "circle_radius_m": 250})
    layers = []
    if show_areas:
        locality_circles = []
        for loc in locs:
            score, _ = locality_score_breakdown(db, loc)
            locality_circles.append({"name": f"{loc.name} locality circle", "locality": loc.name,
                "detail": f"1 km illustrative radius · locality demo score {score}/100",
                "status": "Not a surveyed boundary", "price": "",
                "coordinates": [loc.longitude, loc.latitude]})
        layers.append(pdk.Layer("ScatterplotLayer", data=locality_circles, get_position="coordinates", get_radius=1000,
            radius_units="meters", radius_min_pixels=26, radius_max_pixels=230,
            get_fill_color="[13, 148, 136, 24]", get_line_color="[13, 148, 136, 115]",
            line_width_min_pixels=2, stroked=True, filled=True, pickable=True))
    if show_project_rings and project_data:
        layers.append(pdk.Layer("ScatterplotLayer", data=project_data, get_position="coordinates", get_radius="circle_radius_m",
            radius_units="meters", radius_min_pixels=14, radius_max_pixels=90,
            get_fill_color="[59, 130, 246, 30]", get_line_color="[59, 130, 246, 145]",
            line_width_min_pixels=2, stroked=True, filled=True, pickable=True))
    if project_data:
        layers.append(pdk.Layer("ScatterplotLayer", data=project_data, get_position="coordinates", get_radius=180,
            radius_units="meters", radius_min_pixels=7, radius_max_pixels=18,
            get_fill_color="[14, 138, 129, 220]", get_line_color="[255,255,255,240]", line_width_min_pixels=1, pickable=True, opacity=.9))
    if poi_data:
        layers.append(pdk.Layer("ScatterplotLayer", data=poi_data, get_position="coordinates", get_radius=75,
            get_fill_color="[236, 165, 66, 220]", get_line_color="[255,255,255,230]", pickable=True, opacity=.9))
    zoom = 16.2 if selected_project else 12.5
    lat = selected_project.latitude if selected_project else selected.latitude
    lon = selected_project.longitude if selected_project else selected.longitude
    map_style = os.environ.get("PROPCLUE_MAP_STYLE", "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json")
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom, pitch=20),
        map_style=map_style, tooltip={"html": "<b>{name}</b><br/>{locality}<br/>{detail}<br/>{status}<br/>{price}", "style": {"color": "white"}})
    st.pydeck_chart(deck, width="stretch", height=510, key=f"map-{city}-{selected.name}-{choices[chosen_label] or 'all'}", alt=f"Synthetic {city} project and nearby point map focused on {selected.name}")
    latest_obs = db.scalar(select(MarketObservation).where(MarketObservation.locality_id == selected.id).order_by(MarketObservation.month.desc()).limit(1))
    current_psf, qoq, yoy = locality_appreciation(db, selected.id)
    median_psf = float(pd.Series([p.asking_price / max(p.area_sqft, 1) for p in locality_projects]).median()) if locality_projects else None
    st.markdown(f"#### {selected.name} map snapshot")
    snap = st.columns(4)
    snap[0].metric("Project records", f"{len(locality_projects)}", border=True)
    snap[1].metric("Median project ask", f"₹{median_psf:,.0f}/sq ft" if median_psf is not None else "Unavailable", border=True)
    snap[2].metric("Locality asking trend", f"{qoq:+.1f}% QoQ · {yoy:+.1f}% YoY" if qoq is not None and yoy is not None else "Unavailable", border=True)
    snap[3].metric("Latest demo demand", f"{latest_obs.enquiries:,} enquiries", help=f"Generated availability proxy: {latest_obs.available_units:,} units in the latest monthly observation." if latest_obs else "No generated monthly observation is available.", border=True)
    st.caption("Legend: teal rings are illustrative locality coverage; blue rings are illustrative project areas; solid teal points are project markers; amber points are nearby demo amenities.")
    left, right = st.columns([1, 1])
    with left:
        st.markdown(f"### {selected.name} · {selected.scope}")
        selected_score, _ = locality_score_breakdown(db, selected)
        st.write(f"Locality intelligence demo score **{selected_score}/100** · indicative base asking **{money(selected.base_rate)}/sq ft** · demo rental yield **{selected.rental_yield:.1f}%")
        st.caption("Zooming to a project centers the synthetic project point. No building footprint, parcel boundary, title boundary, tax ledger, approval database or live map feed is connected.")
    with right:
        if st.button("Record locality exploration", type="primary", key="record-map-exploration"):
            add_event(db, user.id, "Map area searched", locality=selected.name, context={"city": city, "scope": selected.scope})
            st.success("Demo map-search event recorded.")
    if selected_project:
        st.markdown(f"#### {selected_project.name} · project snapshot")
        detail_cols = st.columns(4)
        detail_cols[0].metric("Asking price", f"{money(selected_project.asking_price, True)}", border=True)
        detail_cols[1].metric("Asking / sq ft", f"₹{selected_project.asking_price/max(selected_project.area_sqft,1):,.0f}", border=True)
        detail_cols[2].metric("Area · configuration", f"{selected_project.area_sqft:,} sq ft · {selected_project.configuration}", border=True)
        detail_cols[3].metric("Project stage", selected_project.status, border=True)
        st.write("**Selling points · synthetic demo:** " + " · ".join(project_usps(db, selected_project)))
        proximity = project_proximity(selected_project)
        nearby = pd.DataFrame([
            {"Nearby category": "Metro", "Illustrative distance": f"{proximity['metro']:.1f} km"},
            {"Nearby category": "IT park", "Illustrative distance": f"{proximity['it_park']:.1f} km"},
            {"Nearby category": "Business hub", "Illustrative distance": f"{proximity['business_hub']:.1f} km"},
            {"Nearby category": "Hospital", "Illustrative distance": f"{proximity['hospital']:.1f} km"},
            {"Nearby category": "School", "Illustrative distance": f"{proximity['school']:.1f} km"},
            {"Nearby category": "Mall", "Illustrative distance": f"{proximity['mall']:.1f} km"},
            {"Nearby category": "Eatery", "Illustrative distance": f"{proximity['eatery']:.1f} km"},
        ])
        st.dataframe(nearby, width="stretch", hide_index=True, alt="Synthetic straight-line nearby amenity estimates for the selected project")
        st.caption("Distances and nearby points are generated examples, not live routing results. Legal/title/technical clearance, bank approval and property-tax status remain unverified or unconnected.")
    else:
        st.info("Choose a project/building above to reveal its synthetic pricing, selling points, nearby-place estimates and verification checklist.")
    map_hover = []
    for loc in locs:
        score, _ = locality_score_breakdown(db, loc)
        locality_projects = [p for p in projects if p.locality_id == loc.id]
        asking = pd.Series([p.asking_price / max(p.area_sqft, 1) for p in locality_projects]).median() if locality_projects else None
        _, _, loc_yoy = locality_appreciation(db, loc.id)
        details = f"{score}/100 demo score · {len(locality_projects)} project records"
        if asking is not None:
            details += f" · median ask ₹{asking:,.0f}/sq ft"
        if loc_yoy is not None:
            details += f" · generated YoY {loc_yoy:+.1f}%"
        map_hover.append({"id": f"map-locality-{loc.id}", "item_type": "locality", "label": loc.name,
                          "detail": details, "locality": loc.name})
    track_buyer_data(db, user, "map-explorer", map_hover, "Quick scan · hover for 0.7 sec, click or focus a locality card to capture interest")


def show_market(db, city: str, show_header: bool = True):
    if show_header:
        top_header("Market intelligence", "Track asking prices, simulated transactions, rental indicators and supply independently.")
    locs = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    chosen = st.selectbox("Price trend focus", ["City average"] + [x.name for x in locs], key="market_locs")
    rows = db.scalars(select(MarketObservation).join(Locality).where(Locality.city == city).order_by(MarketObservation.month)).all()
    loc_name = {x.id: x.name for x in locs}
    data = pd.DataFrame([{"Month": r.month, "Locality": loc_name[r.locality_id], "Asking ₹/sq ft": r.asking_psf,
                          "Simulated transaction ₹/sq ft": r.transaction_psf, "Rent ₹/sq ft": r.rent_psf,
                          "Available units": r.available_units, "Absorption %": r.absorption_pct,
                          "Buyer enquiries": r.enquiries} for r in rows])
    if chosen == "City average":
        data = data.groupby("Month", as_index=False).agg({"Asking ₹/sq ft": "mean",
                 "Simulated transaction ₹/sq ft": "mean", "Rent ₹/sq ft": "mean",
                 "Available units": "sum", "Absorption %": "mean", "Buyer enquiries": "sum"})
        data["Locality"] = "City average"
    else:
        data = data[data["Locality"] == chosen]
    m1, m2, m3 = st.columns(3)
    m1.metric("Average asking price", money(data["Asking ₹/sq ft"].mean()) + "/sq ft", border=True)
    m2.metric("Avg. simulated transaction", money(data["Simulated transaction ₹/sq ft"].mean()) + "/sq ft", help="Synthetic comparison series; not verified transaction records.", border=True)
    m3.metric("Demo monthly absorption", f"{data['Absorption %'].mean():.1f}%", border=True)
    p1, p2 = st.columns(2, gap="large")
    with p1:
        price = data.melt(id_vars=["Month"], value_vars=["Asking ₹/sq ft", "Simulated transaction ₹/sq ft"], var_name="Series", value_name="₹ / sq ft")
        fig = px.line(price, x="Month", y="₹ / sq ft", color="Series",
                      color_discrete_map={"Asking ₹/sq ft": "#0D9488", "Simulated transaction ₹/sq ft": "#3B82F6"})
        fig.update_traces(line=dict(width=3), mode="lines+markers", marker=dict(size=4))
        fig.update_layout(legend=dict(orientation="h", y=1.14, x=0), hovermode="x unified")
        st.plotly_chart(chart_layout(fig), width="stretch", alt="Asking price and simulated transaction price by locality over 36 months", config={"displayModeBar": False})
    with p2:
        supply = data.groupby(["Locality"], as_index=False).agg({"Available units": "mean", "Buyer enquiries": "mean"})
        supply = supply.melt(id_vars="Locality", var_name="Signal", value_name="Monthly average")
        fig = px.bar(supply, x="Locality", y="Monthly average", color="Signal", barmode="group", title="Supply vs buyer enquiries · demo")
        st.plotly_chart(chart_layout(fig), width="stretch", alt="Synthetic available units compared with buyer inquiries across localities", config={"displayModeBar": False})
    scored_locs = [(x, locality_score_breakdown(db, x)[0]) for x in locs if chosen == "City average" or x.name == chosen]
    yields = pd.DataFrame([{"Locality": x.name, "Demo rental yield %": x.rental_yield, "Locality score": score,
                            "Flood exposure demo score": x.flood_score} for x, score in scored_locs])
    fig = px.scatter(yields, x="Locality score", y="Demo rental yield %", size="Flood exposure demo score", color="Locality",
                     hover_data=["Flood exposure demo score"], title="Locality indicators · synthetic")
    st.plotly_chart(chart_layout(fig, height=290), width="stretch", alt="Synthetic locality score and rental yield with flood exposure size", config={"displayModeBar": False})
    market_hover = [{"id": f"market-locality-{loc.id}", "item_type": "locality", "label": loc.name,
                     "detail": f"{score}/100 demo score · {loc.rental_yield:.1f}% synthetic rental yield",
                     "locality": loc.name} for loc, score in scored_locs]
    track_buyer_data(db, current_user(db), "market-intelligence", market_hover,
                     "Quick scan · hover a locality card to record an opted-in interaction")
    if scored_locs:
        show_locality_methodology(db, scored_locs[0][0])
    st.caption("The 36-month series is generated deterministically. Transaction and asking price fields are never combined.")


def show_builder_land_map(db, city: str, builder: User):
    localities = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    projects = db.scalars(select(Project).where(Project.city == city).order_by(Project.name)).all()
    if not localities:
        st.info("No locality map records are available.")
        return
    chosen = st.selectbox("Area focus", [loc.name for loc in localities], key="builder-land-locality")
    locality = next((loc for loc in localities if loc.name == chosen), localities[0])
    local_projects = [p for p in projects if p.locality_id == locality.id]
    stages = ["Illustrative plot opportunity · availability unverified", "In-progress development", "Completed project"]
    plot_points = []
    for i in range(6):
        seed = int(hashlib.sha256(f"{locality.id}:{i}:land-map".encode()).hexdigest()[:8], 16)
        stage = stages[i % len(stages)]
        plot_points.append({"name": f"Demo site {i+1} · {locality.name}", "locality": locality.name,
            "detail": f"{stage} · illustrative {0.8 + (seed % 95)/10:.1f} acres",
            "owner": "Landowner identity: not connected", "title": "Title / legal clearance: not checked",
            "technical": "Planning / technical clearance: not connected", "coordinates": [locality.longitude + ((seed % 90)-45)/5000, locality.latitude + (((seed//90)%90)-45)/5000], "stage": stage})
    project_points = [{"name": p.name, "locality": locality.name, "detail": f"{p.status} · existing synthetic project",
                       "owner": f"Builder: {p.builder.name} · demo record", "title": "Title / legal clearance: not checked",
                       "technical": "Construction / tax feeds: not connected", "coordinates": [p.longitude, p.latitude], "stage": p.status} for p in local_projects]
    all_data = plot_points + project_points
    layers = [pdk.Layer("ScatterplotLayer", data=all_data, get_position="coordinates", get_radius=100,
                        get_fill_color="[13, 148, 136, 220]", get_line_color="[255,255,255,230]",
                        line_width_min_pixels=1, pickable=True, opacity=.9)]
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=locality.latitude, longitude=locality.longitude, zoom=12.5, pitch=15),
        map_style=os.environ.get("PROPCLUE_MAP_STYLE", "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"),
        tooltip={"html": "<b>{name}</b><br/>{locality}<br/>{detail}<br/>{owner}<br/>{title}<br/>{technical}", "style": {"color": "white"}})
    st.pydeck_chart(deck, width="stretch", height=460, key=f"builder-land-{city}-{locality.id}", alt=f"Illustrative land and development opportunity points in {locality.name}")
    st.caption("Map points are synthetic planning placeholders, not surveyed plots or parcels. Owner names, title searches, encumbrances, land use, tax status, technical clearances, and acquisition availability are not connected. Verify through authorized records before engaging or acquiring land.")
    st.subheader("Area context")
    if local_projects:
        st.dataframe(pd.DataFrame([{"Existing demo project": p.name, "Builder": p.builder.name, "Stage": p.status,
                                    "Legal/title": "Not checked", "Tax / approvals": "Not connected"} for p in local_projects]), width="stretch", hide_index=True)
    else:
        st.info("No existing demo project points in this locality. Synthetic opportunity markers still do not represent available parcels.")


def show_builder_market_workspace(db, user: User, city: str):
    top_header("Market intelligence", f"Market conditions, acquisition context and pricing scenarios for {city}.")
    tabs = st.tabs(["Market signals", "Land & development map", "Portfolio pricing guidance"])
    with tabs[0]:
        show_market(db, city, show_header=False)
    with tabs[1]:
        show_builder_land_map(db, city, user)
    with tabs[2]:
        projects = db.scalars(select(Project).where(Project.builder_id == user.builder_id, Project.city == city).order_by(Project.name)).all()
        if not projects:
            st.info("No portfolio projects are available in this market.")
            return
        labels = {f"{p.name} · {p.locality.name}": p for p in projects}
        chosen = st.selectbox("Portfolio project", list(labels), key="builder-pricing-project")
        project = labels[chosen]
        signal = pricing_guidance(db, project)
        st.markdown(f"### {signal['action']}")
        st.write(signal["why"])
        a, b, c, d = st.columns(4)
        a.metric("Project asking", f"₹{project.asking_price/max(project.area_sqft,1):,.0f}/sq ft", border=True)
        b.metric("Locality ask · generated", f"₹{signal['latest_ask']:,.0f}/sq ft", border=True)
        c.metric("Relative premium", f"{signal['premium']:+.1f}%", border=True)
        d.metric("Recent enquiries / available unit", f"{signal['demand_supply']:.2f}", border=True)
        st.caption(f"Rule inputs: latest 3 locality observations ({signal['demand']:.0f} demo enquiries and {signal['supply']:.0f} available-unit proxy on average), relative asking price, and {signal['usp_count']} synthetic USP tags.")
        st.info("This is a directional demo scenario, not an automated pricing recommendation. Economic conditions, interest rates, competitor offers, actual absorption, unit-level inventory, and verified buyer feedback are not connected.")
        pricing_rows = []
        for p in projects:
            item = pricing_guidance(db, p)
            pricing_rows.append({"Project": p.name, "Locality": p.locality.name, "Ask premium vs locality": f"{item['premium']:+.1f}%", "Generated demand/supply": f"{item['demand_supply']:.2f}", "Scenario cue": item["action"]})
        st.dataframe(pd.DataFrame(pricing_rows), width="stretch", hide_index=True, alt="Illustrative builder pricing scenario cues based on synthetic locality signals")


def builder_leads(db, builder_id: int, city: str | None = None):
    stmt = select(Lead, User, Project).join(User, Lead.buyer_id == User.id).join(Project, Lead.project_id == Project.id).where(Lead.builder_id == builder_id)
    rows = db.execute(stmt).all()
    consent_map = latest_consents(db, [buyer.id for _, buyer, _ in rows])
    contact_consent_map = latest_consents(db, [buyer.id for _, buyer, _ in rows])
    records = []
    for lead, buyer, project in rows:
        if city and buyer.city != city:
            continue
        activities = db.scalars(select(LeadActivity).where(LeadActivity.lead_id == lead.id).order_by(LeadActivity.created_at)).all()
        initial = next((item for item in activities if item.action in {"Buyer enquiry submitted", "Partner referral submitted"}), None)
        records.append({"Lead": lead, "Buyer": buyer, "Project record": project,
                        "Buyer name": buyer.name, "City": buyer.city, "Locality": buyer.locality,
                        "Budget": f"{money(buyer.budget_min, True)}–{money(buyer.budget_max, True)}",
                        "Configuration": buyer.configuration, "Timeline": buyer.timeline,
                        "Project": project.name, "Intent score": lead.intent_score,
                        "Intent": lead.intent_category, "Last activity": lead.last_activity.strftime("%d %b %Y"),
                        "Marketing eligible": consent_map.get((buyer.id, "builder_marketing"), False), "Status": lead.status,
                        "Next action": "Confirm visit" if lead.status == "Site visit requested" else ("Offer a site visit" if lead.intent_category == "High" else "Share project details"),
                        "Source": ("Channel partner referral" if initial and initial.action == "Partner referral submitted" else "Buyer-submitted demo request") if initial else "Seed sample lead",
                        "Contact preference": initial.contact_channel if initial else "—",
                        "Contact email": buyer.email if initial and initial.share_contact and contact_consent_map.get((buyer.id, "builder_contact"), False) else "Not shared / withdrawn",
                        "Requested documents": ", ".join(json.loads(initial.requested_documents_json or "[]")) if initial else "—",
                        "Activities": activities})
    return records


def workflow_is_operational(record: dict) -> bool:
    return record["Source"] != "Seed sample lead"


def lead_followup_due(record: dict, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    lead = record["Lead"]
    if lead.status in {"Booked", "Closed - lost", "Closed - cancelled", "Site visit completed"}:
        return False
    deadlines = [activity.due_at for activity in record["Activities"] if activity.due_at]
    if workflow_is_operational(record):
        return bool(deadlines and max(deadlines) <= now)
    return lead.status in {"New", "Contact pending"} or (now - lead.last_activity).days > 7


def show_builder_inventory(db, user: User, city: str) -> None:
    projects = db.scalars(select(Project).where(Project.builder_id == user.builder_id, Project.city == city).order_by(Project.name)).all()
    st.subheader("Project inventory")
    st.caption("Unit states can be updated in this demo, but every sample row remains marked synthetic and unverified. A real inventory integration should replace these placeholders.")
    if not projects:
        st.info("No projects are assigned to this builder in the selected market.")
        return
    project_names = {f"{project.name} · {project.locality.name}": project for project in projects}
    selected_label = st.selectbox("Project inventory", list(project_names), key="workflow-inventory-project")
    project = project_names[selected_label]
    units = db.scalars(select(ProjectInventory).where(ProjectInventory.project_id == project.id).order_by(ProjectInventory.unit_label)).all()
    if units:
        counts = {state: sum(row.availability == state for row in units) for state in ["Available", "Reserved", "Sold"]}
        inventory_metrics = st.columns(3)
        for column, state in zip(inventory_metrics, ["Available", "Reserved", "Sold"]):
            column.metric(state, counts[state], border=True)
        st.dataframe(pd.DataFrame([{"Unit / sample": row.unit_label, "State": row.availability,
            "Indicative ask": money(row.asking_price, True), "Source": row.source_name,
            "Updated": row.updated_at.strftime("%d %b %Y %H:%M UTC"), "Verification": row.verification_status}
            for row in units]), width="stretch", hide_index=True, alt=f"Sample inventory units for {project.name}")
        with st.form("builder-inventory-update"):
            unit_labels = {row.unit_label: row for row in units}
            selected_unit = st.selectbox("Sample unit", list(unit_labels))
            new_state = st.selectbox("Availability", ["Available", "Reserved", "Sold"])
            if st.form_submit_button("Save demo inventory update"):
                row = unit_labels[selected_unit]
                row.availability = new_state
                row.source_name = "Builder demo workflow"
                row.verification_status = "Demo entry · not externally verified"
                row.updated_at = datetime.utcnow()
                db.add(AuditLog(actor_id=user.id, action="demo_inventory_updated",
                    detail=f"project={project.id}; unit={row.unit_label}; state={new_state}"))
                db.commit()
                st.success("Demo inventory state updated and timestamped.")
    else:
        st.info("No sample units exist for this project.")


def show_channel_partner(db, user: User) -> None:
    profile = db.scalar(select(ChannelPartnerProfile).where(ChannelPartnerProfile.user_id == user.id))
    top_header("Channel partner workspace", "Submit consent-based buyer referrals to your approved builder portfolio.")
    if not profile or not profile.builder_id or profile.status not in {"Approved", "Approved · demo", "Approved · local demo"}:
        st.info("Your partner account is awaiting builder affiliation approval. Referrals are locked until an admin approves the account and assigns a builder.")
        return
    builder = db.get(Builder, profile.builder_id)
    st.caption(f"{profile.company_name} · assigned builder: {builder.name if builder else '—'}")
    projects = db.scalars(select(Project).where(Project.builder_id == profile.builder_id).order_by(Project.name)).all()
    if not projects:
        st.info("No projects are available under the assigned builder.")
        return
    project_options = {f"{project.name} · {project.locality.name}": project for project in projects}
    st.subheader("Create a buyer referral")
    st.caption("Only submit a person who has explicitly asked you to share their details with this builder. This demo stores the referral; it sends no notification.")
    with st.form("partner-referral-form"):
        project_label = st.selectbox("Project", list(project_options))
        buyer_name = st.text_input("Buyer name", max_chars=100)
        buyer_email = st.text_input("Buyer email", max_chars=180)
        requested_visit = st.checkbox("Buyer requested a site visit")
        visit_date = st.date_input("Preferred visit date", value=date.today() + timedelta(days=3))
        visit_time = st.time_input("Preferred visit time", value=datetime.strptime("11:00", "%H:%M").time())
        referral_note = st.text_area("Referral context", max_chars=500)
        buyer_consent = st.checkbox("The buyer has consented to share these details with the selected builder for follow-up.")
        submit_referral = st.form_submit_button("Submit referral to builder", type="primary")
    if submit_referral:
        email = buyer_email.strip().lower()
        if not buyer_name.strip() or "@" not in email or not buyer_consent:
            st.error("Enter the buyer's name and a valid email, and confirm their consent before submitting.")
            return
        project = project_options[project_label]
        buyer = db.scalar(select(User).where(User.email == email))
        if buyer and buyer.role != "buyer":
            st.error("That email is already associated with another role. Ask the buyer to use their buyer account email.")
            return
        if not buyer:
            buyer = User(role="buyer", name=buyer_name.strip(), email=email, city=project.city,
                         locality=project.locality.name)
            db.add(buyer)
            db.flush()
        now = datetime.utcnow()
        lead = Lead(builder_id=project.builder_id, buyer_id=buyer.id, project_id=project.id,
                    status="New", intent_score=0, intent_category="Low", last_activity=now,
                    assigned_to="Unassigned")
        db.add(lead)
        db.flush()
        db.add(LeadActivity(lead_id=lead.id, actor_id=user.id, action="Partner referral submitted",
            status_after="New", note=referral_note.strip(), contact_channel="Email", share_contact=True,
            due_at=now + timedelta(hours=24),
            appointment_at=datetime.combine(visit_date, visit_time) if requested_visit else None,
            created_at=now))
        db.add(ConsentRecord(user_id=buyer.id, purpose="builder_contact", granted=True,
            recorded_at=now, source="Channel partner referral consent"))
        db.add(AuditLog(actor_id=user.id, action="partner_referral_submitted",
            detail=f"lead={lead.id}; project={project.id}; partner={user.id}"))
        db.commit()
        st.success(f"Referral routed to {builder.name}. Request ID: PC-{lead.id:06d}.")
    st.subheader("My referrals")
    activity_rows = db.scalars(select(LeadActivity).where(LeadActivity.actor_id == user.id,
        LeadActivity.action == "Partner referral submitted").order_by(LeadActivity.created_at.desc())).all()
    referral_rows = []
    for activity in activity_rows:
        lead = db.get(Lead, activity.lead_id)
        if lead and lead.builder_id == profile.builder_id:
            buyer = db.get(User, lead.buyer_id)
            project = db.get(Project, lead.project_id)
            referral_rows.append({"Request": f"PC-{lead.id:06d}", "Buyer": buyer.name if buyer else "—",
                "Project": project.name if project else "—", "Status": lead.status,
                "Submitted": activity.created_at.strftime("%d %b %Y %H:%M UTC"),
                "Updated": lead.last_activity.strftime("%d %b %Y %H:%M UTC")})
    if referral_rows:
        st.dataframe(pd.DataFrame(referral_rows), width="stretch", hide_index=True, alt="Channel partner buyer referrals and builder follow-up status")
    else:
        st.info("No referrals submitted yet.")


def render_lead_timeline(record: dict) -> None:
    activities = record["Activities"]
    if not activities:
        st.caption("No workflow history yet · seed sample lead")
        return
    for activity in activities:
        details = f"{activity.created_at.strftime('%d %b %Y %H:%M UTC')} · {activity.action} · {activity.status_after}"
        if activity.note:
            details += f" · {activity.note}"
        if activity.appointment_at:
            details += f" · visit {activity.appointment_at.strftime('%d %b %Y %H:%M')}"
        if activity.due_at:
            details += f" · response due {activity.due_at.strftime('%d %b %H:%M UTC')}"
        st.caption(details)


def show_leads(db, user: User, city: str):
    builder = db.get(Builder, user.builder_id)
    top_header("Buyer demand & campaigns", f"{builder.name if builder else 'Your portfolio'} · follow-up, segmentation and soft campaign workspace.")
    records = builder_leads(db, user.builder_id, city)
    workflow_records = [r for r in records if workflow_is_operational(r)]
    response_hours = []
    for record in workflow_records:
        start = next((a for a in record["Activities"] if a.action in {"Buyer enquiry submitted", "Partner referral submitted"}), None)
        response = next((a for a in record["Activities"] if a.action in {"Contact attempted", "Contacted"}), None)
        if start and response:
            response_hours.append(max(0, (response.created_at - start.created_at).total_seconds() / 3600))
    metric_cols = st.columns(5)
    metric_cols[0].metric("Workflow leads", len(workflow_records), help="Buyer enquiries and channel-partner referrals; excludes seeded sample leads.", border=True)
    metric_cols[1].metric("Avg. first response", f"{sum(response_hours)/len(response_hours):.1f} h" if response_hours else "No responses yet", help="Calculated only from buyer-submitted demo requests with a recorded first contact.", border=True)
    metric_cols[2].metric("Visits scheduled / done", sum(r["Status"] in {"Site visit scheduled", "Site visit completed"} for r in workflow_records), border=True)
    metric_cols[3].metric("Bookings", sum(r["Status"] == "Booked" for r in workflow_records), border=True)
    metric_cols[4].metric("Closed / cancelled", sum(r["Status"] in {"Closed - lost", "Closed - cancelled"} for r in workflow_records), border=True)
    st.caption("Campaign delivery receipts are not connected. A live Twilio test is a single-destination test, not a per-buyer campaign delivery.")
    contacted = [r for r in workflow_records if any(a.action in {"Contact attempted", "Contacted"} for a in r["Activities"])]
    visited = [r for r in workflow_records if any(a.action in {"Site visit scheduled", "Site visit completed"} for a in r["Activities"])]
    contacted_visits = [r for r in contacted if any(a.action in {"Site visit scheduled", "Site visit completed"} for a in r["Activities"])]
    visited_bookings = [r for r in visited if r["Status"] == "Booked"]
    conversion_cols = st.columns(2)
    conversion_cols[0].metric("Recorded contact → visit", f"{len(contacted_visits) / len(contacted) * 100:.1f}%" if contacted else "No contact records", help=f"{len(contacted_visits)} of {len(contacted)} contacted workflow leads have a scheduled/completed visit record.", border=True)
    conversion_cols[1].metric("Recorded visit → booking", f"{len(visited_bookings) / len(visited) * 100:.1f}%" if visited else "No visit records", help=f"{len(visited_bookings)} of {len(visited)} leads with a visit record are marked booked.", border=True)
    tab_leads, tab_inventory, tab_campaigns = st.tabs(["Lead pipeline", "Project inventory", "Segments & soft campaigns"])
    with tab_leads:
        if not records:
            st.info("No leads match this builder portfolio in the selected market.")
        else:
            p1, p2, p3 = st.columns(3)
            localities = sorted({r["Locality"] for r in records})
            selected_loc = p1.selectbox("Locality", ["All"] + localities, key="builder-lead-locality")
            statuses = sorted({r["Status"] for r in records})
            selected_status = p2.selectbox("Follow-up status", ["All"] + statuses, key="builder-lead-status")
            followup_view = p3.selectbox("Work queue", ["All leads", "Follow-up due", "Active in last 7 days", "Site visit / negotiation"], key="builder-lead-queue")
            now = datetime.utcnow()
            filtered = [r for r in records if (selected_loc == "All" or r["Locality"] == selected_loc)
                        and (selected_status == "All" or r["Status"] == selected_status)
                        and (followup_view != "Follow-up due" or lead_followup_due(r, now))
                        and (followup_view != "Active in last 7 days" or (now-r["Lead"].last_activity).days <= 7)
                        and (followup_view != "Site visit / negotiation" or r["Status"] in {"Site visit scheduled", "Negotiation"})]
            opted = sum(r["Marketing eligible"] for r in filtered)
            c1, c2, c3 = st.columns(3)
            c1.metric("Leads in view", len(filtered), border=True)
            c2.metric("Follow-up due", sum(lead_followup_due(r, now) for r in filtered), border=True)
            c3.metric("Marketing consent present", opted, help="Only synthetic consent-eligible records can enter a campaign simulation.", border=True)
            display = pd.DataFrame([{k: r[k] for k in ["Buyer name", "City", "Locality", "Budget", "Configuration", "Timeline", "Project", "Source", "Contact preference", "Contact email", "Requested documents", "Last activity", "Marketing eligible", "Status", "Next action"]} for r in filtered])
            st.dataframe(display, width="stretch", hide_index=True, height=420, alt="Builder authorized synthetic buyer leads, activity status, consent and suggested next action")
            st.markdown("#### Work a lead")
            lead_options = {f"PC-{r['Lead'].id:06d} · {r['Buyer name']} · {r['Project']} · {r['Status']} · {r['Source']}": r for r in filtered}
            if lead_options:
                lead_label = st.selectbox("Select lead", list(lead_options), key="work-lead-choice")
                selected_record = lead_options[lead_label]
                selected_lead = selected_record["Lead"]
                with st.expander("Contact visibility and history", expanded=True):
                    st.write(f"**Contact:** {selected_record['Contact email']} · **Preferred channel:** {selected_record['Contact preference']}")
                    st.write(f"**Requested documents:** {selected_record['Requested documents']}")
                    render_lead_timeline(selected_record)
                with st.form(f"lead-update-{selected_lead.id}"):
                    statuses = ["New", "Contact pending", "Contact attempted", "Contacted", "Interested", "Qualified", "Site visit requested", "Site visit scheduled", "Site visit completed", "Negotiation", "Booked", "Closed - lost", "Closed - cancelled"]
                    current_status = selected_lead.status if selected_lead.status in statuses else "New"
                    new_status = st.selectbox("Pipeline status", statuses, index=statuses.index(current_status))
                    owner = st.text_input("Assigned owner", value=selected_lead.assigned_to or "Unassigned")
                    actions = ["Contact attempted", "Contacted", "Qualified", "Site visit scheduled", "Site visit completed", "Negotiation", "Booking confirmed", "Closed - lost", "Closed - cancelled", "Document request handled", "Follow-up scheduled"]
                    action = st.selectbox("Activity to record", actions)
                    due_enabled = st.checkbox("Set response / follow-up deadline", value=False)
                    due_date = st.date_input("Follow-up date", value=date.today() + timedelta(days=1))
                    due_time = st.time_input("Follow-up time", value=datetime.strptime("17:00", "%H:%M").time())
                    appointment_enabled = st.checkbox("Record / update site visit time", value=False)
                    appointment_date = st.date_input("Site visit date", value=date.today() + timedelta(days=3))
                    appointment_time = st.time_input("Site visit time", value=datetime.strptime("11:00", "%H:%M").time())
                    update_note = st.text_area("Outcome / next step note", max_chars=500)
                    save_update = st.form_submit_button("Save lead update", type="primary")
                if save_update:
                    now = datetime.utcnow()
                    selected_lead.status = new_status
                    selected_lead.assigned_to = owner.strip() or "Unassigned"
                    selected_lead.last_activity = now
                    db.add(LeadActivity(lead_id=selected_lead.id, actor_id=user.id, action=action,
                        status_after=new_status, note=update_note.strip(),
                        due_at=datetime.combine(due_date, due_time) if due_enabled else None,
                        appointment_at=datetime.combine(appointment_date, appointment_time) if appointment_enabled else None,
                        created_at=now))
                    db.add(AuditLog(actor_id=user.id, action="lead_workflow_updated",
                        detail=f"lead={selected_lead.id}; status={new_status}; action={action}"))
                    db.commit()
                    st.success("Lead status, owner, follow-up, and activity history updated.")
            with st.expander("Export consent-eligible leads"):
                eligible = [r for r in filtered if r["Marketing eligible"]]
                if eligible:
                    export = pd.DataFrame([{k: r[k] for k in ["Buyer name", "City", "Locality", "Budget", "Configuration", "Timeline", "Project", "Status"]} for r in eligible])
                    st.download_button("Download eligible demo leads CSV", export.to_csv(index=False), "propclue-eligible-demo-leads.csv", "text/csv")
                else:
                    st.info("No buyers in this view have builder-marketing consent.")
    with tab_inventory:
        show_builder_inventory(db, user, city)
    with tab_campaigns:
        if not records:
            st.info("No portfolio leads are available for segmentation.")
        else:
            localities = ["Any locality"] + sorted({r["Locality"] for r in records})
            projects = ["Any project"] + sorted({r["Project"] for r in records})
            c1, c2, c3 = st.columns(3)
            locality_filter = c1.selectbox("Locality segment", localities, key="campaign-locality")
            project_filter = c2.selectbox("Project segment", projects, key="campaign-project")
            recency_days = c3.selectbox("Recent activity", ["Any time", "Last 7 days", "Last 30 days", "Last 90 days"], index=2, key="campaign-recency")
            days = {"Any time": 100000, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[recency_days]
            segment = [r for r in records if r["Marketing eligible"]
                       and (locality_filter == "Any locality" or r["Locality"] == locality_filter)
                       and (project_filter == "Any project" or r["Project"] == project_filter)
                       and (datetime.utcnow()-r["Lead"].last_activity).days <= days]
            st.markdown("#### Campaign agent")
            st.caption("A reviewable, multi-step campaign workflow inspired by the Utkarsh demo. The agent prepares a soft-campaign preview; it does not contact buyers.")
            stage_cols = st.columns(4)
            stage_data = [
                ("01 · Audience", f"{len(records)} lead records", "Filters by locality, project and recent activity."),
                ("02 · Consent gate", f"{len(segment)} eligible", "Excludes anyone without builder-marketing consent."),
                ("03 · Channel", "Choose below", "SMS, WhatsApp or voice bot preview."),
                ("04 · Outcome", "Simulation only", "Illustrative responses; no delivery or send."),
            ]
            for col, (title, value, detail) in zip(stage_cols, stage_data):
                with col.container(border=True):
                    st.caption(title)
                    st.markdown(f"**{value}**")
                    st.caption(detail)
            st.metric("Consent-eligible audience", len(segment), help="Filtered to buyers with synthetic builder-marketing consent.", border=True)
            channel = st.radio("Campaign channel", ["SMS · Twilio", "WhatsApp · Twilio", "Voice bot · Twilio"], horizontal=True, key="campaign-channel")
            campaign_name = st.text_input("Campaign name", value=f"{city} buyer update · demo", key="soft-campaign-name")
            message = st.text_area("Draft", value="Hello, we have a project update that may match your stated preferences. Would you like more details?", key="soft-campaign-message")
            twilio_config = twilio_live_config(channel)
            account_sid, auth_token, sender, receiver, send_pin = twilio_config
            channel_ready = bool(account_sid and auth_token and sender and receiver and send_pin)
            if receiver:
                try:
                    masked_receiver = f"ending {normalize_e164(receiver)[-4:]}"
                except ValueError:
                    masked_receiver = "configured number is invalid"
            else:
                masked_receiver = "not configured"
            sender_config_key = "TWILIO_WHATSAPP_FROM" if channel.startswith("WhatsApp") else "TWILIO_FROM_NUMBER"
            missing_config = [name for name, value in [
                ("TWILIO_ACCOUNT_SID", account_sid), ("TWILIO_AUTH_TOKEN", auth_token),
                (sender_config_key, sender), ("CAMPAIGN_RECEIVER_PHONE", receiver), ("CAMPAIGN_SEND_PIN", send_pin),
            ] if not value]
            if missing_config:
                st.caption(f"Live {channel.split(' ·')[0]} setup incomplete. Add private Streamlit secrets: {', '.join(missing_config)}.")
            else:
                st.caption(f"Live test destination: phone {masked_receiver}. All selected synthetic leads resolve to this single test destination.")
            st.info("Campaign preview keeps the synthetic audience statistics. A live run sends at most **one** message or call to the configured test number; it never sends one copy per synthetic lead.")
            if st.button("Run campaign agent preview", type="primary", disabled=not segment, key="simulate-unified-campaign"):
                clicks = int(round(len(segment) * random.Random(len(segment) + len(campaign_name) + len(channel)).uniform(.12, .38)))
                campaign = Campaign(builder_id=user.builder_id, name=campaign_name.strip() or "Untitled demo campaign",
                    segment=f"{channel} · {locality_filter} · {project_filter} · activity {recency_days} · consent eligible",
                    audience_size=len(records), eligible_count=len(segment), simulated_clicks=clicks, status="Simulated")
                db.add(campaign)
                db.add(AuditLog(actor_id=user.id, action="campaign_simulated", detail=campaign.name))
                db.commit()
                st.success(f"Campaign agent complete · audience checked: {len(records)} · consent eligible: {len(segment)} · channel: {channel.split(' ·')[0]} · illustrative responses: {clicks}. Nothing was sent.")
            with st.expander("Live Twilio delivery · one test message or call"):
                st.caption(f"This action contacts only the configured test destination ({masked_receiver}). It does not contact the synthetic buyer records.")
                entered_pin = st.text_input("Private campaign send PIN", type="password", key="campaign-live-pin")
                pin_ok = bool(send_pin and entered_pin and hmac.compare_digest(entered_pin, send_pin))
                confirmed = st.checkbox(f"I confirm one live {channel.split(' ·')[0]} test to phone {masked_receiver} now.", key="campaign-live-confirm")
                cooldown_until = st.session_state.get("campaign_live_cooldown_until")
                still_in_cooldown = bool(cooldown_until and datetime.utcnow() < cooldown_until)
                live_enabled = channel_ready and pin_ok and confirmed and bool(segment) and bool(message.strip()) and not still_in_cooldown
                if not pin_ok and entered_pin:
                    st.error("The campaign send PIN does not match.")
                if still_in_cooldown:
                    st.warning("A live test was just sent from this session. Wait five minutes before sending another.")
                if st.button("Send one live test via Twilio", type="primary", disabled=not live_enabled, key="send-live-twilio-test"):
                    try:
                        twilio_result = twilio_send_one(channel, message.strip(), twilio_config)
                        db.add(AuditLog(actor_id=user.id, action="campaign_live_test_sent",
                                        detail=f"{campaign_name.strip() or 'Untitled demo campaign'} · {channel.split(' ·')[0]} · {twilio_result['status']}"))
                        db.commit()
                        st.session_state.campaign_live_cooldown_until = datetime.utcnow() + timedelta(minutes=5)
                        st.success(f"Twilio accepted one live {channel.split(' ·')[0]} test to phone {masked_receiver}. Delivery status: {twilio_result['status']}.")
                    except (ValueError, RuntimeError, requests.RequestException) as exc:
                        st.error(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Twilio could not be reached. Check the private account configuration and try again.")
            campaigns = db.scalars(select(Campaign).where(Campaign.builder_id == user.builder_id).order_by(Campaign.created_at.desc())).all()
            st.subheader("Previous soft campaign simulations")
            st.dataframe(pd.DataFrame([{"Campaign": c.name, "Segment / channel": c.segment, "Eligible": c.eligible_count,
                "Illustrative responses": c.simulated_clicks, "Status": c.status, "Created": c.created_at.strftime("%d %b %Y")} for c in campaigns]), width="stretch", hide_index=True, alt="Saved synthetic soft campaign summaries")


def show_segments(db, user: User, city: str):
    top_header("Segments & campaigns", "Build an opted-in demo audience and simulate a campaign. No messages are sent.")
    records = builder_leads(db, user.builder_id, city)
    if not records:
        st.info("No leads are available in this view.")
        return
    p1, p2, p3 = st.columns(3)
    segment_locality = p1.selectbox("Locality segment", ["Any"] + sorted({r["Locality"] for r in records}))
    segment_config = p2.selectbox("Configuration", ["Any", "1 BHK", "2 BHK", "3 BHK", "4 BHK"])
    min_intent = p3.selectbox("Minimum intent", ["Low", "Medium", "High"], index=1)
    rank = {"Low": 0, "Medium": 1, "High": 2}
    segment = [r for r in records if r["Marketing eligible"] and rank[r["Intent"]] >= rank[min_intent]
               and (segment_locality == "Any" or r["Locality"] == segment_locality)
               and (segment_config == "Any" or r["Configuration"] == segment_config)]
    st.metric("Consent-eligible segment size", len(segment), help="Consent is checked before audience inclusion.")
    st.dataframe(pd.DataFrame([{k: r[k] for k in ["Buyer name", "Locality", "Budget", "Configuration", "Intent score", "Intent"]} for r in segment]).head(30), width="stretch", hide_index=True, alt="Consent-eligible demo audience for the selected campaign segment")
    campaign_name = st.text_input("Campaign draft name", value=f"{city} buyer interest · demo")
    message = st.text_area("Outreach draft (not sent)", value="Hello, we have a project update that may match your saved preferences. Would you like more details?")
    st.caption("Draft content is saved only as a simulated campaign. This demo never sends WhatsApp, SMS or email.")
    if st.button("Run campaign simulation", type="primary", disabled=not segment):
        clicks = int(round(len(segment) * random.Random(len(segment) + len(campaign_name)).uniform(.18, .42)))
        campaign = Campaign(builder_id=user.builder_id, name=campaign_name.strip() or "Untitled demo campaign",
                            segment=f"{city} · {segment_locality} · {segment_config} · {min_intent}+ · opted in",
                            audience_size=len(records), eligible_count=len(segment), simulated_clicks=clicks,
                            status="Simulated")
        db.add(campaign)
        db.add(AuditLog(actor_id=user.id, action="campaign_simulated", detail=campaign.name))
        db.commit()
        st.success(f"Simulation complete: {len(segment)} eligible demo buyers, {clicks} illustrative clicks. No message was sent.")
    st.subheader("Previous simulations")
    campaigns = db.scalars(select(Campaign).where(Campaign.builder_id == user.builder_id).order_by(Campaign.created_at.desc())).all()
    st.dataframe(pd.DataFrame([{"Campaign": c.name, "Segment": c.segment, "Eligible": c.eligible_count,
                                "Simulated clicks": c.simulated_clicks,
                                "Demo click rate": f"{(c.simulated_clicks / c.eligible_count * 100 if c.eligible_count else 0):.1f}%",
                                "Status": c.status, "Created": c.created_at.strftime("%d %b %Y")} for c in campaigns]), width="stretch", hide_index=True, alt="Campaign simulations with consent-eligible audience and illustrative clicks")


def assistant_answer(db, user: User, city: str, question: str) -> str:
    q = question.lower().strip()
    locality_rows = db.scalars(select(Locality).where(Locality.city == city)).all()
    all_projects = db.scalars(select(Project)).all()
    projects_in_market = [p for p in all_projects if p.city == city]
    legal_terms = ("dispute", "title", "khata", "encumbrance", "bank", "approval letter", "legal", "tax", "rera", "document", "clearance")
    if any(term in q for term in legal_terms) or re.search(r"\bec\b", q):
        project = None
        selected_id = st.session_state.get("selected_project")
        candidate = db.get(Project, selected_id) if selected_id else None
        if candidate and candidate.city == city:
            project = candidate
        if not project:
            normalized_question = re.sub(r"[^a-z0-9]+", " ", q).strip()
            project = next((p for p in all_projects if re.sub(r"[^a-z0-9]+", " ", p.name.lower()).strip() in normalized_question), None)
        if not project:
            return "I can’t verify legal or bank status from this demo. Choose a project in Discover or Map, then ask again. No EC, Khata, title, RERA, tax, dispute or bank-letter source is connected, so I can’t call any property dispute-free. Legal/title scores are not assessed."
        return (f"For **{project.name}**, the demo has no official document or approval integrations. Dispute-free status is **not verified**; EC, Khata, title chain, RERA/approvals, tax receipts and technical clearance are **not connected**. Legal/title score: **not assessed**. Bank approval: **not verified**, and no bank approval letter is attached. Please obtain and independently verify certified documents before making a decision.")
    psf_query = re.search(r"(?:per\s*(?:sq\.?\s*ft|sft|sqft|square\s*(?:foot|feet))|psf)", q)
    limit_match = re.search(r"(?:under|below|less\s+than|up\s+to|upto|at\s+most|max(?:imum)?)\s*(?:₹|rs\.?\s*)?([\d,]+(?:\.\d+)?)\s*(k|thousand|lakh|lac|l|cr|crore)?", q)
    explicit_total_budget = any(term in q for term in ("total price", "overall price", "budget", "property value", "lakh", "lac", "crore", " cr"))
    implicit_psf_query = bool(limit_match and float(limit_match.group(1).replace(",", "")) < 100_000
                             and limit_match.group(2) in {None, "k", "thousand"}
                             and not explicit_total_budget)
    if (psf_query or implicit_psf_query) and limit_match:
        amount = float(limit_match.group(1).replace(",", ""))
        unit = (limit_match.group(2) or "").lower()
        amount *= {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lac": 100_000,
                   "l": 100_000, "cr": 10_000_000, "crore": 10_000_000}.get(unit, 1)
        matches = sorted([p for p in projects_in_market if p.asking_price / max(p.area_sqft, 1) <= amount],
                         key=lambda p: p.asking_price / max(p.area_sqft, 1))
        if not matches:
            return f"I couldn’t find a synthetic {city} project below ₹{amount:,.0f}/sq ft. Try a higher cap or another locality. Asking prices are generated demo values, not live listings."
        lines = [f"**{p.name}** · {p.locality.name} · {p.property_type}, {p.configuration} · ₹{p.asking_price / max(p.area_sqft, 1):,.0f}/sq ft · {money(p.asking_price, True)} total" for p in matches[:8]]
        more = f" Showing 8 of {len(matches)} matches." if len(matches) > 8 else ""
        return (f"I found **{len(matches)} synthetic {city} projects** at or below **₹{amount:,.0f}/sq ft**:{more}\n\n"
                + "\n\n".join(lines)
                + "\n\nThese are generated comparison records, not verified or currently available properties. Asking price per sq ft is calculated as total asking price ÷ listed area.")

    def project_match(text: str) -> tuple[Project | None, bool]:
        normalized_question = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        normalized_names = {p.id: re.sub(r"[^a-z0-9]+", " ", p.name.lower()).strip() for p in all_projects}
        exact = [p for p in all_projects if normalized_names[p.id] and normalized_names[p.id] in normalized_question]
        if exact:
            return exact[0], True
        stop = {"give", "me", "show", "property", "properties", "project", "details", "detail", "for", "fro", "about", "tell", "info", "information", "please", "the", "a", "an", "of", "in", "at", "what", "is", "are", "can", "you", "to", "this", "that", "and", "with"}
        query_tokens = set(normalized_question.split()) - stop
        ranked = []
        for p in all_projects:
            name_tokens = set(normalized_names[p.id].split())
            overlap = query_tokens & name_tokens
            coverage = len(overlap) / max(len(name_tokens), 1)
            numeric_match = bool(name_tokens & query_tokens & {t for t in name_tokens if t.isdigit()})
            if coverage >= .6 or (numeric_match and len(overlap) >= 2):
                ranked.append((coverage + (0.1 if numeric_match else 0), p))
        ranked.sort(key=lambda row: row[0], reverse=True)
        if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            return ranked[0][1], False
        return None, False

    asked_for_details = any(term in q for term in ("details", "detail", "information", "tell me about", "specification", "specs", "property info", "project info", "full info", "more about"))
    matched_project, exact_match = project_match(q)
    if matched_project and (asked_for_details or exact_match):
        p = matched_project
        psf = p.asking_price / max(p.area_sqft, 1)
        yield_pct = p.monthly_rent * 12 / max(p.asking_price, 1) * 100
        current_psf, qoq, yoy = locality_appreciation(db, p.locality_id)
        proximity = project_proximity(p)
        amenities = json.loads(p.amenities_json or "[]")
        legal = legal_evidence_status(p)
        match_note = "" if exact_match else f"I couldn’t find that exact title; the closest synthetic match is **{p.name}**.\n\n"
        trend = f"{qoq:+.1f}% QoQ · {yoy:+.1f}% YoY" if qoq is not None and yoy is not None else "Not enough generated history"
        return (f"{match_note}### {p.name}\n"
                f"- **Builder:** {p.builder.name}\n- **Location:** {p.locality.name}, {p.city}\n"
                f"- **Type / configuration:** {p.property_type} · {p.configuration}\n- **Area:** {p.area_sqft:,} sq ft\n"
                f"- **Synthetic asking price:** {money(p.asking_price, True)} ({money(psf)}/sq ft)\n"
                f"- **Simulated transaction comparison:** {money(p.transaction_price, True)} (generated, not a verified sale)\n"
                f"- **Demo rent / yield:** {money(p.monthly_rent)}/month · {yield_pct:.1f}%\n"
                f"- **Stage / possession field:** {p.status} · {p.possession}\n- **Locality asking trend:** {trend}\n"
                f"- **Project highlights:** {', '.join(project_usps(db, p))}\n- **Listed demo amenities:** {', '.join(amenities) if amenities else 'Not listed'}\n"
                f"- **Illustrative distances:** Metro {proximity['metro']} km · IT park {proximity['it_park']} km · Hospital {proximity['hospital']} km · School {proximity['school']} km\n"
                f"- **Legal / bank evidence:** EC, Khata, title, RERA, tax and technical clearances are not connected; bank approval is not verified.\n\n"
                "Prices, rent, trend, amenities and distances are synthetic demo fields. Verify project availability, documents, approvals and all commercial details independently.")
    if user.role == "builder":
        if "high intent" in q or "buyer" in q or "lead" in q:
            rows = sorted(builder_leads(db, user.builder_id, city), key=lambda row: row["Intent score"], reverse=True)[:5]
            if not rows:
                return "Your demo builder account has no authorized leads in this market. Try the other city filter."
            return "Your top authorized demo buyers are " + ", ".join(f"{r['Buyer name']} ({r['Intent score']}/100, {r['Locality']})" for r in rows) + ". Scores and profiles are synthetic."
        if "project" in q or "inventory" in q:
            own = db.scalars(select(Project).where(Project.builder_id == user.builder_id, Project.city == city)).all()
            return f"Your highest asking-price demo project is {max(own, key=lambda p: p.asking_price).name}." if own else f"No projects are assigned to your builder in {city}."
        if "why" in q:
            return "Demo buyer-intent scores combine enquiries, contact requests, shortlists, comparisons, report downloads, repeat views and recency. Recent activity decays by half every 30 days. Sensitive attributes are excluded."
    if "appreciation" in q or "qoq" in q or "yoy" in q or "price" in q:
        loc = next((x for x in locality_rows if x.name.lower() in q), None)
        loc = loc or (db.get(Project, st.session_state.get("selected_project")).locality if st.session_state.get("selected_project") and db.get(Project, st.session_state.get("selected_project")) else None)
        if loc:
            current, qoq, yoy = locality_appreciation(db, loc.id)
            return f"{loc.name} synthetic asking-price series: latest ₹{current:,.0f}/sq ft; QoQ {qoq:+.1f}% and YoY {yoy:+.1f}% versus the same locality’s earlier generated observations. This is not verified transaction data." if current is not None and qoq is not None and yoy is not None else f"There is not enough demo history to calculate appreciation for {loc.name}."
    selected_id = st.session_state.get("selected_project")
    selected_project = db.get(Project, selected_id) if selected_id else None
    if selected_project and any(word in q for word in ("usp", "near", "metro", "school", "hospital", "it park", "selling point")):
        prox = project_proximity(selected_project)
        return f"{selected_project.name} highlights: {'; '.join(project_usps(db, selected_project))}. Illustrative straight-line distances: metro {prox['metro']} km, IT park {prox['it_park']} km, hospital {prox['hospital']} km, school {prox['school']} km. These are synthetic, not live routes."
    return "Ask about a project’s USP or demo legal checklist, a locality’s QoQ/YoY trend, nearby highlights, or select a project to get a focused answer."


def show_assistant(db, user: User, city: str, with_header: bool = True, floating: bool = False):
    if with_header:
        top_header("PropClue assistant", "Ask in plain language about a project, market trend, documents or nearby amenities.")
    st.caption("Answers query the synthetic demo database. No external LLM is called; legal and bank checks are not connected.")
    conv_key = f"conversation_id_{user.id}"
    if conv_key not in st.session_state:
        conversation = AIConversation(user_id=user.id, title="Demo property questions")
        db.add(conversation)
        db.commit()
        st.session_state[conv_key] = conversation.id
    cid = st.session_state[conv_key]
    messages = db.scalars(select(AIMessage).where(AIMessage.conversation_id == cid).order_by(AIMessage.created_at)).all()
    message_area = st.container(height=340 if floating else None) if floating else st.container()
    with message_area:
        for message in messages[-20:]:
            with st.chat_message(message.role):
                st.markdown(message.content)
    if floating:
        with st.form(f"floating-chat-form-{user.id}", clear_on_submit=True):
            question = st.text_input("Ask PropClue", placeholder="Ask about a project, price, or documents…", label_visibility="collapsed")
            submitted = st.form_submit_button("Send message", type="primary", width="stretch")
        if submitted and question.strip():
            answer = assistant_answer(db, user, city, question.strip())
            db.add_all([AIMessage(conversation_id=cid, role="user", content=question.strip()), AIMessage(conversation_id=cid, role="assistant", content=answer)])
            db.commit()
            st.rerun()
        st.caption("Demo answers use synthetic data. Legal and bank checks are not verified.")
        with st.expander("Try a prompt"):
            st.write("Properties under ₹10k/sq ft? · Details for Devanahalli Residences 42 · Is this property dispute-free? · Show EC and Khata status · What is the YoY appreciation? · What are this project's USPs?")
        return
def show_activity(db, user: User):
    top_header("My activity", "A timeline of demo events attached to your buyer account.")
    rows = db.scalars(select(Event).where(Event.buyer_id == user.id).order_by(Event.event_at.desc()).limit(80)).all()
    st.metric("Recorded demo activities", db.scalar(select(func.count(Event.id)).where(Event.buyer_id == user.id)) or 0)
    if rows:
        st.dataframe(pd.DataFrame([{"When": r.event_at.strftime("%d %b %Y · %H:%M"), "Event": r.event_type,
                                    "City": r.city, "Locality": r.locality,
                                    "Project": db.get(Project, r.project_id).name if r.project_id and db.get(Project, r.project_id) else "—",
                                    "Module / item": (lambda c: f"{c.get('module', '—')} · {c.get('label', '')}" if r.event_type == "Buyer data hovered" else "—")(json.loads(r.context_json or "{}")),
                                    "Consent processing": r.consent_status} for r in rows]), width="stretch", hide_index=True, alt="Recent recorded buyer activity in the demo account")
    else:
        st.info("Your demo activity timeline is empty. Explore or save a project to add an event.")


def show_saved(db, user: User, show_header: bool = True):
    if show_header:
        top_header("Compare & saved", "Review the projects you saved and compare up to four synthetic records.")
    saved = db.scalars(select(SavedProperty).where(SavedProperty.user_id == user.id).order_by(SavedProperty.saved_at.desc())).all()
    saved_projects = [db.get(Project, x.project_id) for x in saved if db.get(Project, x.project_id)]
    if saved_projects:
        st.dataframe(project_frame(saved_projects).drop(columns=["Project ID"]), width="stretch", hide_index=True, alt="Saved synthetic projects and market indicators")
        hover_items = [{"id": f"saved-project-{p.id}", "item_type": "saved project", "project_id": p.id,
                        "label": p.name, "detail": f"{p.locality.name} · {p.configuration} · {money(p.asking_price, True)} asking",
                        "locality": p.locality.name} for p in saved_projects[:8]]
        track_buyer_data(db, user, "compare-saved", hover_items, "Quick scan · hover a saved project card to record an opted-in interaction")
    else:
        st.info("No saved projects yet. Save one from Discover.")
    all_projects = db.scalars(select(Project).where(Project.city == st.session_state.get("city_filter", user.city))).all()
    labels = {f"{p.name} · {p.locality.name} · {money(p.asking_price, True)}": p for p in all_projects}
    picked = st.multiselect("Choose up to 4 projects", list(labels), max_selections=4, key="comparison_projects")
    if len(picked) >= 2:
        comps = [labels[x] for x in picked]
        compare = pd.DataFrame([{"Measure": "Asking price", **{p.name: money(p.asking_price, True) for p in comps}},
                                {"Measure": "Simulated transaction", **{p.name: money(p.transaction_price, True) for p in comps}},
                                {"Measure": "Area", **{p.name: f"{p.area_sqft:,} sq ft" for p in comps}},
                                {"Measure": "Configuration", **{p.name: p.configuration for p in comps}},
                                {"Measure": "Locality score", **{p.name: locality_score_breakdown(db, p.locality)[0] for p in comps}},
                                {"Measure": "Demo rental yield", **{p.name: f"{p.monthly_rent * 12 / p.asking_price * 100:.1f}%" for p in comps}},
                                {"Measure": "Verification", **{p.name: "Synthetic demo record" for p in comps}}])
        st.dataframe(compare, width="stretch", hide_index=True, alt="Side-by-side comparison of synthetic projects")
        if st.button("Save comparison history", type="primary"):
            db.add(ProjectComparison(user_id=user.id, project_ids_json=json.dumps([p.id for p in comps])))
            for project in comps:
                add_event(db, user.id, "Property compared", project)
            db.commit()
            st.success("Comparison saved to your demo account.")


def show_buyer_requests(db, user: User) -> None:
    st.subheader("My requests and visits")
    leads = db.scalars(select(Lead).where(Lead.buyer_id == user.id).order_by(Lead.last_activity.desc())).all()
    leads = [lead for lead in leads if db.scalar(select(func.count(LeadActivity.id)).where(
        LeadActivity.lead_id == lead.id, LeadActivity.action.in_(["Buyer enquiry submitted", "Partner referral submitted"])))]
    if not leads:
        st.info("You have not sent a builder enquiry yet. Open a project in Discover projects to request a callback, documents, or a visit.")
        return
    for lead in leads:
        project = db.get(Project, lead.project_id)
        activities = db.scalars(select(LeadActivity).where(LeadActivity.lead_id == lead.id).order_by(LeadActivity.created_at)).all()
        initial = next((activity for activity in activities if activity.action in {"Buyer enquiry submitted", "Partner referral submitted"}), None)
        with st.container(border=True):
            st.markdown(f"### PC-{lead.id:06d} · {project.name if project else 'Project'}")
            st.write(f"**Builder:** {project.builder.name if project else '—'} · **Status:** {lead.status} · **Assigned to:** {lead.assigned_to}")
            if initial:
                st.caption(f"Preferred follow-up: {initial.contact_channel or 'Not selected'} · email sharing: {'Allowed for this enquiry' if initial.share_contact else 'Not shared'}")
                docs = json.loads(initial.requested_documents_json or "[]")
                st.write("**Requested documents:** " + (", ".join(docs) if docs else "None"))
                if initial.appointment_at:
                    st.write(f"**Preferred site visit:** {initial.appointment_at.strftime('%d %b %Y · %H:%M')}")
                if initial.due_at:
                    st.caption(f"Builder response target · {initial.due_at.strftime('%d %b %Y · %H:%M UTC')}")
            for activity in activities:
                st.caption(f"{activity.created_at.strftime('%d %b %Y · %H:%M UTC')} · {activity.action} · {activity.status_after}" + (f" · {activity.note}" if activity.note else ""))


def show_privacy(db, user: User):
    top_header("Privacy & consent", "Review purpose-specific demo preferences. Every change is stored as a timestamped consent record.")
    current = latest_consents(db, [user.id])
    purposes = {"analytics": "Product analytics", "personalization": "Personalized discovery",
                "builder_marketing": "Builder marketing campaigns",
                "builder_contact": "Share contact details on builder enquiries"}
    st.info("You can withdraw builder contact sharing here. The builder then sees the request status and history, but the account email is hidden. This demo stores preference records locally; production consent handling and retention require approved policy and controls.")
    with st.form("consent_form"):
        choices = {key: st.checkbox(label, value=current.get((user.id, key), False), key=f"consent-{key}") for key, label in purposes.items()}
        submit = st.form_submit_button("Save consent choices", type="primary")
    if submit:
        now = datetime.utcnow()
        for key, granted in choices.items():
            db.add(ConsentRecord(user_id=user.id, purpose=key, granted=granted, recorded_at=now))
        db.add(AuditLog(actor_id=user.id, action="consent_updated", detail=json.dumps(choices)))
        db.commit()
        st.success("Your preferences were saved with a timestamp.")
    rows = db.scalars(select(ConsentRecord).where(ConsentRecord.user_id == user.id).order_by(ConsentRecord.recorded_at.desc())).all()
    st.subheader("Consent history")
    st.dataframe(pd.DataFrame([{"Purpose": r.purpose.replace("_", " ").title(), "Granted": "Yes" if r.granted else "No",
                                "Recorded at": r.recorded_at.strftime("%d %b %Y · %H:%M UTC"), "Source": r.source} for r in rows]), width="stretch", hide_index=True, alt="Timestamped privacy and marketing consent history")


def show_coverage(db):
    top_header("Data coverage & licensing", "See what is synthetic, what is connected and what needs an authorized source.")
    sources = db.scalars(select(DataSource).order_by(DataSource.priority, DataSource.dataset)).all()
    df = pd.DataFrame([{"Dataset": x.dataset, "Category": x.category, "Candidate source": x.source_name,
                        "Type": x.source_type, "Coverage": x.coverage, "Licensing": x.licensing_status,
                        "Verification": x.verification_status, "Priority": x.priority,
                        "Demo substitute": x.demo_substitute} for x in sources])
    connected = int(df["Licensing"].str.contains("connected|demo-only", case=False).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Tracked datasets", len(df))
    c2.metric("Connected real sources", 0)
    c3.metric("Synthetic demo sources", connected)
    st.dataframe(df, width="stretch", hide_index=True, height=500, alt="Data coverage, licensing status, verification and procurement priority")
    st.subheader("Procurement shortlist")
    st.write("1. **Critical:** state-authorized transaction, land/legal, and RERA/project-approval data.\n2. **High:** licensed building footprints, parcels, builder inventory, rental listings and POIs.\n3. **Medium:** environmental and infrastructure observations with refresh cadence and permitted-use terms.")
    st.caption("Potential suppliers are not connected or endorsed. Confirm licence, geography, update cadence, and permitted use before procurement.")


def show_admin(db, user: User):
    top_header("Platform overview", "Synthetic demo operations, provenance and a data reset control.")
    buyers = db.scalar(select(func.count(User.id)).where(User.role == "buyer")) or 0
    projects = db.scalar(select(func.count(Project.id))) or 0
    events = db.scalar(select(func.count(Event.id))) or 0
    leads = db.scalar(select(func.count(Lead.id))) or 0
    a, b, c, d = st.columns(4)
    a.metric("Synthetic buyers", f"{buyers:,}")
    b.metric("Projects", f"{projects:,}")
    c.metric("Activity records", f"{events:,}")
    d.metric("Demo leads", f"{leads:,}")
    roles = pd.DataFrame([{"Role": role, "Accounts": count} for role, count in db.execute(select(User.role, func.count(User.id)).group_by(User.role)).all()])
    fig = px.bar(roles, x="Role", y="Accounts", color="Role", title="Synthetic account coverage")
    st.plotly_chart(chart_layout(fig, height=300), width="stretch", alt="Number of synthetic demo accounts by platform role", config={"displayModeBar": False})
    st.subheader("Recent audit activity")
    audits = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(25)).all()
    st.dataframe(pd.DataFrame([{"Action": x.action, "Detail": x.detail, "At": x.created_at.strftime("%d %b %Y · %H:%M UTC")} for x in audits]), width="stretch", hide_index=True, alt="Recent platform audit actions")
    st.warning("Reset drops the demo tables and regenerates synthetic records. It never touches a production database. Confirm before resetting.")
    confirm = st.checkbox("I understand this resets the demo database", key="reset_confirm")
    if st.button("Reset demo data", disabled=not confirm):
        reset_demo_data(db)
        for key in ["user_id", "conversation_id", "selected_project"]:
            st.session_state.pop(key, None)
        st.success("Demo data reset. Sign in again to continue.")
        st.rerun()


def show_market_workspace(db, user: User, city: str):
    top_header("Market overview", f"Market pulse and deeper intelligence for {city}.")
    view = st.segmented_control("Market view", ["Market pulse", "Market intelligence"], default="Market pulse", key="market-workspace-tab")
    if view == "Market intelligence":
        show_market(db, city, show_header=False)
    else:
        show_overview(db, user, city, show_header=False)


def show_property_workspace(db, user: User, city: str):
    top_header("Explore properties", "Discover, inspect the map, compare and save from one workspace.")
    view = st.segmented_control("Property view", ["Discover projects", "Map explorer", "Compare & save", "My requests"], default="Discover projects", key="property-workspace-tab")
    if view == "Map explorer":
        show_map(db, user, city, show_header=False)
    elif view == "Compare & save":
        show_saved(db, user, show_header=False)
    elif view == "My requests":
        show_buyer_requests(db, user)
    else:
        show_discover(db, user, city, show_header=False)


def main():
    with SessionLocal() as db:
        user = current_user(db)
        if not user:
            login_screen(db)
            return
        with st.sidebar:
            brand_lockup(compact=True)
            st.caption(f"{user.name} · {user.role.title()}")
            city = get_city_filter(db, user)
            if st.button("Sign out", width="stretch"):
                st.session_state.pop("user_id", None)
                st.rerun()
            st.markdown("---")
        page_groups = {
            "buyer": [
                st.Page(lambda: show_market_workspace(db, user, city), title="Market overview", url_path="market-overview", icon=":material/monitoring:"),
                st.Page(lambda: show_property_workspace(db, user, city), title="Explore properties", url_path="explore-properties", icon=":material/map:"),
            ],
            "builder": [
                st.Page(lambda: show_builder_portfolio(db, user, city), title="Portfolio MIS", url_path="portfolio-overview", icon=":material/monitoring:"),
                st.Page(lambda: show_builder_market_workspace(db, user, city), title="Market intelligence", url_path="builder-market", icon=":material/trending_up:"),
                st.Page(lambda: show_leads(db, user, city), title="Buyer demand & campaigns", url_path="buyer-demand-leads", icon=":material/group:"),
            ],
            "channel_partner": [
                st.Page(lambda: show_channel_partner(db, user), title="Partner workspace", url_path="partner-workspace", icon=":material/handshake:")
            ],
            "admin": [
                st.Page(lambda: show_admin(db, user), title="Platform overview", url_path="platform-overview", icon=":material/space_dashboard:"),
                st.Page(lambda: show_coverage(db), title="Data coverage & licensing", url_path="data-coverage-licensing", icon=":material/database:"),
                st.Page(lambda: show_map(db, user, city), title="Map explorer", url_path="admin-map", icon=":material/map:"),
                st.Page(lambda: show_market(db, city), title="Market intelligence", url_path="admin-market", icon=":material/trending_up:"),
            ],
        }
        role_pages = page_groups.get(user.role, page_groups["buyer"])
        navigation = st.navigation({user.role.title(): role_pages}, position="hidden")
        with st.sidebar:
            st.markdown("**WORKSPACE MODULES**")
            for page in role_pages:
                st.page_link(page)
            st.markdown("---")
            st.markdown("**LLM TOKEN USAGE**")
            st.table(pd.DataFrame([
                {"Stage": "Input", "Tokens": 0},
                {"Stage": "Output", "Tokens": 0},
                {"Stage": "Intent", "Tokens": 0},
                {"Stage": "SQL generation", "Tokens": 0},
                {"Stage": "AI insights", "Tokens": 0},
            ]))
            st.caption("Zero external LLM calls in this demo.")
            st.caption("SYNTHETIC DEMO · not verified")
        st.markdown("""
        <style>
        div[data-testid="stPopover"] { position: fixed !important; display: inline-flex !important; width: max-content !important; max-width: calc(100vw - 1.5rem) !important; left: auto !important; right: 1.5rem !important; bottom: 1.25rem !important; z-index: 99999 !important; }
        div[data-testid="stPopover"] button, button[title="Open the PropClue property assistant"] { display: inline-flex !important; width: 220px !important; min-width: 220px !important; min-height: 64px !important; justify-content: center !important; border-radius: 999px !important; padding: .8rem 1.45rem !important; box-shadow: 0 10px 32px rgba(14, 116, 109, .38) !important; font-weight: 800 !important; font-size: 1.2rem !important; border: 2px solid rgba(255,255,255,.9) !important; }
        div[data-testid="stPopover"] button p, button[title="Open the PropClue property assistant"] p { font-size: 1.2rem !important; font-weight: 800 !important; }
        div[data-testid="stPopoverBody"] { width: min(410px, calc(100vw - 2rem)) !important; }
        @media (max-width: 640px) { div[data-testid="stPopover"] { right: .75rem !important; bottom: .75rem !important; } div[data-testid="stPopover"] button, button[title="Open the PropClue property assistant"] { width: 190px !important; min-width: 190px !important; min-height: 56px !important; font-size: 1.05rem !important; } }
        </style>
        """, unsafe_allow_html=True)
        with st.popover("💬  Ask PropClue", type="primary", help="Open the PropClue property assistant"):
            st.markdown("#### Your property assistant")
            st.caption("Ask about projects, prices, market trends, or document checks.")
            show_assistant(db, user, city, with_header=False, floating=True)
        navigation.run()


main()
