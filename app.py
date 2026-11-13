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
import plotly.graph_objects as go
import pydeck as pdk
import requests
import streamlit as st
from sqlalchemy import select, func
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

# Streamlit Cloud secrets are exposed before database.py creates its engine.
try:
    if "DATABASE_URL" in st.secrets and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
except Exception:
    pass

import database as database_module
from database import (
    AIConversation, AIMessage, AuditLog, Builder, Campaign, ConsentRecord, DataSource,
    ChannelPartnerProfile, Event, Lead, LeadActivity, Locality, MarketObservation,
    Project, ProjectComparison, ProjectEvidence, ProjectInventory, SavedProperty,
    SessionLocal, User, init_db, reset_demo_data, score_from_events,
)

try:
    configure_temporary_fallback = database_module.configure_temporary_fallback
except AttributeError:
    # A running Streamlit worker can briefly retain an older imported database
    # module after a hot reload. Keep that worker compatible with this app.
    def configure_temporary_fallback() -> str:
        import tempfile
        from pathlib import Path
        from sqlalchemy import create_engine

        fallback_path = Path(tempfile.gettempdir()) / "propclue_demo_fallback.db"
        fallback_url = f"sqlite:///{fallback_path.as_posix()}"
        database_module.engine.dispose()
        database_module.engine = create_engine(
            fallback_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        database_module.SessionLocal.configure(bind=database_module.engine)
        database_module.DB_URL = fallback_url
        return str(fallback_path)

from source_data import clear_source_cache, load_area_dna, load_catalog
try:
    from source_data import load_builder_directory
except ImportError:
    # Keep older Streamlit workers running during a hot reload after this
    # optional directory query was added. A restart loads the full function.
    def load_builder_directory(city_slug: str) -> pd.DataFrame:
        return pd.DataFrame(columns=["rank", "builder_name", "data_confidence", "city_slug"])


st.set_page_config(page_title="PropClue | Real estate intelligence", page_icon="⌂", layout="wide",
                   initial_sidebar_state="expanded")


DATABASE_FALLBACK_NOTICE = ""


def boot_database() -> bool:
    global DATABASE_FALLBACK_NOTICE
    # Re-run create_all on every Streamlit rerun so an existing local/cloud DB
    # picks up newly added tables without requiring a manual reset.
    try:
        init_db()
    except OperationalError:
        # Streamlit Cloud may have a missing, sleeping, or temporarily
        # unreachable DATABASE_URL. Keep the demo usable while making the
        # non-persistent fallback explicit.
        if "propclue_demo_fallback.db" in database_module.DB_URL:
            raise
        configure_temporary_fallback()
        init_db()
        DATABASE_FALLBACK_NOTICE = (
            "The configured demo database is temporarily unavailable. "
            "This session is using temporary demo storage; changes may not persist."
        )
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


_MARKET_PULSE_COMPONENT = st.components.v2.component(
    "propclue_market_pulse_clock",
    html="""
<section class="pc-pulse" aria-label="PropClue live market pulse">
  <div class="pc-pulse-head">
    <div><div class="pc-eyebrow">PROPCLUE / MARKET SIGNALS</div><div class="pc-pulse-title">Market pulse</div></div>
    <div class="pc-live"><span class="pc-live-dot"></span> CATALOGUE SNAPSHOT</div>
  </div>
  <div class="pc-pulse-body">
    <div class="pc-dial-wrap">
      <svg class="pc-dial" viewBox="0 0 360 360" role="img" aria-label="Live moving clock dial for market indicators">
        <circle class="pc-ring pc-ring-outer" cx="180" cy="180" r="158" />
        <circle class="pc-ring pc-ring-inner" cx="180" cy="180" r="111" />
        <g class="pc-ticks"></g><g class="pc-sector-labels"></g>
        <line class="pc-hand-shadow" x1="180" y1="192" x2="180" y2="57" />
        <line class="pc-hand" x1="180" y1="192" x2="180" y2="57" />
        <line class="pc-hand-tail" x1="180" y1="192" x2="180" y2="220" />
        <circle class="pc-hub" cx="180" cy="180" r="12" />
        <circle class="pc-hub-core" cx="180" cy="180" r="4" />
        <text class="pc-clock-time" x="180" y="248" text-anchor="middle">--:--:--</text>
        <text class="pc-clock-label" x="180" y="266" text-anchor="middle">MARKET CLOCK · IST</text>
      </svg>
      <div class="pc-dial-foot"><span>Asia/Kolkata</span><span class="pc-foot-dot"></span><span>Clock ticks continuously</span></div>
    </div>
    <div class="pc-signals">
      <div class="pc-signal-intro"><span>CONNECTED CATALOGUE SNAPSHOT</span><span class="pc-snapshot"></span></div>
      <div class="pc-kpis"></div>
      <div class="pc-secondary"></div>
      <div class="pc-source-note"><span class="pc-source-icon">⌁</span><span>Connected PropClue PostgreSQL catalogue · figures reflect stored listing records, not verified transactions or available inventory. Refresh the source view to reload data.</span></div>
    </div>
  </div>
</section>
""",
    css="""
.pc-pulse { box-sizing: border-box; width: 100%; overflow: hidden; color: #eff8ff; border: 1px solid #234356; border-radius: 24px; padding: 1.25rem 1.4rem 1.15rem; background: radial-gradient(ellipse at 22% 46%, rgba(13,148,136,.18), transparent 38%), radial-gradient(ellipse at 86% 8%, rgba(42,91,125,.18), transparent 36%), linear-gradient(125deg,#081b2c 0%,#0c2638 54%,#102b3a 100%); box-shadow: 0 24px 55px rgba(7,30,48,.18), inset 0 1px 0 rgba(255,255,255,.06); font-family: Inter,ui-sans-serif,system-ui,sans-serif; }
.pc-pulse * { box-sizing: border-box; }
.pc-pulse-head { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:0 .15rem 1rem; border-bottom:1px solid rgba(177,210,226,.12); }
.pc-eyebrow { font-size:.65rem; letter-spacing:.19em; font-weight:700; color:#66d7cc; }
.pc-pulse-title { margin-top:.28rem; font-size:1.32rem; letter-spacing:-.035em; font-weight:690; color:#f4f9fd; }
.pc-live { display:flex; align-items:center; gap:.5rem; border:1px solid rgba(95,227,179,.22); border-radius:99px; padding:.42rem .72rem; color:#a7efd3; background:rgba(11,84,78,.24); font-size:.61rem; font-weight:700; letter-spacing:.11em; white-space:nowrap; }
.pc-live-dot,.pc-foot-dot { width:7px; height:7px; border-radius:50%; background:#5fe3b3; box-shadow:0 0 0 4px rgba(95,227,179,.12),0 0 13px rgba(95,227,179,.7); }
.pc-pulse-body { display:grid; grid-template-columns:1fr; gap:1.25rem; align-items:center; padding:.9rem .25rem .15rem; }
.pc-dial-wrap { min-width:0; display:none !important; align-items:center; flex-direction:column; }
.pc-dial { display:block; width:min(100%,370px); height:auto; overflow:visible; }
.pc-ring { fill:none; stroke:rgba(198,223,235,.17); stroke-width:1; }
.pc-ring-inner { stroke:rgba(198,223,235,.29); stroke-dasharray:2 7; }
.pc-tick { stroke:rgba(225,240,247,.42); stroke-width:1.2; stroke-linecap:round; }
.pc-tick-major { stroke:#86b9c3; stroke-width:2; }
.pc-sector-label { fill:#abc5d1; font-size:8.7px; letter-spacing:.15em; font-weight:700; text-anchor:middle; dominant-baseline:middle; }
.pc-sector-dot { fill:#ff7b78; filter:drop-shadow(0 0 4px rgba(255,123,120,.8)); }
.pc-hand-shadow { stroke:rgba(0,0,0,.55); stroke-width:8; stroke-linecap:round; transform-origin:180px 180px; }
.pc-hand { stroke:#ff777d; stroke-width:3.2; stroke-linecap:round; transform-origin:180px 180px; filter:drop-shadow(0 0 7px rgba(255,119,125,.6)); }
.pc-hand-tail { stroke:#ff777d; stroke-width:2.5; stroke-linecap:round; transform-origin:180px 180px; }
.pc-hub { fill:#183c4c; stroke:rgba(212,239,244,.26); stroke-width:1; }
.pc-hub-core { fill:#ff777d; }
.pc-clock-time { fill:#eff8ff; font-size:17px; font-weight:550; letter-spacing:.06em; font-variant-numeric:tabular-nums; }
.pc-clock-label { fill:#7fa5b7; font-size:7px; font-weight:700; letter-spacing:.18em; }
.pc-dial-foot { display:flex; align-items:center; justify-content:center; gap:.5rem; color:#82a4b3; font-size:.66rem; margin-top:-.35rem; text-align:center; }
.pc-foot-dot { width:4px; height:4px; box-shadow:none; }
.pc-signals { min-width:0; }
.pc-signal-intro { display:flex; justify-content:space-between; align-items:center; gap:1rem; margin-bottom:.7rem; color:#7fa5b7; font-size:.61rem; font-weight:700; letter-spacing:.15em; }
.pc-snapshot { color:#a8cad3; font-size:.68rem; letter-spacing:.02em; font-variant-numeric:tabular-nums; }
.pc-kpis { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.65rem; }
.pc-kpi { min-width:0; min-height:88px; padding:.78rem .85rem .7rem; border:1px solid rgba(175,210,226,.13); border-radius:15px; background:linear-gradient(145deg,rgba(255,255,255,.065),rgba(255,255,255,.025)); }
.pc-kpi-label { display:flex; align-items:center; gap:.42rem; color:#91b1c1; font-size:.68rem; font-weight:600; }
.pc-kpi-mark { width:6px; height:6px; border-radius:2px; background:#5fe3b3; }
.pc-kpi-value { color:#f2f8fb; font-size:1.48rem; line-height:1.12; letter-spacing:-.045em; font-weight:650; margin-top:.42rem; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.pc-kpi-detail { color:#789aaa; font-size:.65rem; margin-top:.2rem; }
.pc-secondary { display:flex; flex-wrap:wrap; gap:.46rem; margin-top:.72rem; }
.pc-chip { display:flex; align-items:center; gap:.35rem; border:1px solid rgba(161,201,218,.14); border-radius:99px; padding:.38rem .58rem; background:rgba(255,255,255,.035); color:#b7ccd4; font-size:.67rem; }
.pc-chip strong { color:#f0f7fa; font-weight:650; }
.pc-chip-muted strong { color:#f8bfad; }
.pc-source-note { display:flex; gap:.55rem; margin-top:.8rem; padding-top:.7rem; border-top:1px solid rgba(177,210,226,.12); color:#7696a6; font-size:.65rem; line-height:1.45; }
.pc-source-icon { color:#66d7cc; font-size:1rem; line-height:1; }
@media(max-width:760px) { .pc-pulse { border-radius:18px; padding:1rem .85rem; } .pc-pulse-body { grid-template-columns:1fr; gap:.6rem; padding-top:.65rem; } .pc-dial { width:min(84vw,330px); } .pc-pulse-head { align-items:flex-start; } .pc-live { font-size:.54rem; padding:.35rem .5rem; } .pc-pulse-title { font-size:1.18rem; } .pc-kpi { min-height:78px; padding:.68rem; } .pc-kpi-value { font-size:1.28rem; } .pc-signal-intro { align-items:flex-start; flex-direction:column; gap:.3rem; } }
@media(prefers-reduced-motion:reduce) { .pc-live-dot { box-shadow:0 0 0 3px rgba(95,227,179,.12); } }
""",
    js="""
export default function (component) {
  const { data, parentElement } = component
  const root = parentElement.querySelector('.pc-pulse')
  if (!root) return
  const kpis = parentElement.querySelector('.pc-kpis')
  const secondary = parentElement.querySelector('.pc-secondary')
  const timeNode = parentElement.querySelector('.pc-clock-time')
  const snapshotNode = parentElement.querySelector('.pc-snapshot')
  const ticks = parentElement.querySelector('.pc-ticks')
  const labels = parentElement.querySelector('.pc-sector-labels')
  if (!kpis || !secondary || !timeNode || !snapshotNode || !ticks || !labels) return

  const ns = 'http://www.w3.org/2000/svg'
  if (!ticks.childElementCount) {
    for (let i = 0; i < 48; i++) {
      const angle = (i / 48) * Math.PI * 2 - Math.PI / 2
      const major = i % 8 === 0
      const r1 = major ? 135 : 140
      const r2 = 148
      const line = document.createElementNS(ns, 'line')
      line.setAttribute('x1', String(180 + Math.cos(angle) * r1))
      line.setAttribute('y1', String(180 + Math.sin(angle) * r1))
      line.setAttribute('x2', String(180 + Math.cos(angle) * r2))
      line.setAttribute('y2', String(180 + Math.sin(angle) * r2))
      line.setAttribute('class', major ? 'pc-tick pc-tick-major' : 'pc-tick')
      ticks.append(line)
    }
    const names = ['PRODUCT','LOCATION','SUPPLY','DEMAND','PRICE','PROGRESS']
    names.forEach((name, i) => {
      const angle = (i / names.length) * Math.PI * 2 - Math.PI / 2
      const x = 180 + Math.cos(angle) * 159
      const y = 180 + Math.sin(angle) * 159
      const text = document.createElementNS(ns, 'text')
      text.setAttribute('x', String(x))
      text.setAttribute('y', String(y))
      text.setAttribute('class', 'pc-sector-label')
      text.textContent = name
      labels.append(text)
      const dot = document.createElementNS(ns, 'circle')
      dot.setAttribute('cx', String(180 + Math.cos(angle) * 158))
      dot.setAttribute('cy', String(180 + Math.sin(angle) * 158))
      dot.setAttribute('r', '2.1')
      dot.setAttribute('class', 'pc-sector-dot')
      labels.append(dot)
    })
  }

  kpis.replaceChildren()
  for (const item of (data?.kpis || [])) {
    const card = document.createElement('div')
    card.className = 'pc-kpi'
    const label = document.createElement('div')
    label.className = 'pc-kpi-label'
    const mark = document.createElement('span')
    mark.className = 'pc-kpi-mark'
    const labelText = document.createElement('span')
    labelText.textContent = item.label || ''
    label.append(mark, labelText)
    const value = document.createElement('div')
    value.className = 'pc-kpi-value'
    value.textContent = item.value || '—'
    const detail = document.createElement('div')
    detail.className = 'pc-kpi-detail'
    detail.textContent = item.detail || ''
    card.append(label, value, detail)
    kpis.append(card)
  }
  secondary.replaceChildren()
  for (const item of (data?.chips || [])) {
    const chip = document.createElement('div')
    chip.className = item.muted ? 'pc-chip pc-chip-muted' : 'pc-chip'
    const label = document.createElement('span')
    label.textContent = item.label || ''
    const strong = document.createElement('strong')
    strong.textContent = item.value || ''
    chip.append(label, strong)
    secondary.append(chip)
  }

  const formatTime = () => new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  }).format(new Date())
  let frame = 0
  const hand = parentElement.querySelector('.pc-hand')
  const shadow = parentElement.querySelector('.pc-hand-shadow')
  const tail = parentElement.querySelector('.pc-hand-tail')
  let lastSecond = -1
  const animate = (now) => {
    const seconds = (Date.now() / 1000) % 60
    const rotation = seconds * 6
    const transform = `rotate(${rotation}deg)`
    if (hand) hand.style.transform = transform
    if (shadow) shadow.style.transform = transform
    if (tail) tail.style.transform = transform
    const second = Math.floor(seconds)
    if (second !== lastSecond) {
      lastSecond = second
      timeNode.textContent = formatTime()
      snapshotNode.textContent = `LOCAL CLOCK · ${formatTime()} IST`
    }
    frame = requestAnimationFrame(animate)
  }
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) frame = requestAnimationFrame(animate)
  else { timeNode.textContent = formatTime(); snapshotNode.textContent = `LOCAL CLOCK · ${formatTime()} IST` }
  return () => cancelAnimationFrame(frame)
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
    for label, key in [
        ("metro", "metro"), ("public_transport", "public transport"), ("it_park", "IT park"),
        ("hospital", "hospital"), ("mall", "mall"), ("school", "school"),
        ("petrol_bunk", "petrol bunk"), ("airport", "airport"),
        ("railway_station", "railway station"), ("business_hub", "business hub"), ("eatery", "eatery"),
    ]:
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


@st.cache_data(ttl=120)
def live_market_labels() -> dict[str, str]:
    """Market selector options from cities present in the live PropClue data."""
    localities, listings = load_catalog()
    labels = {
        str(row.city_slug): str(row.city)
        for row in localities[["city_slug", "city"]].dropna().drop_duplicates().itertuples(index=False)
    }
    for slug in listings["city_slug"].dropna().astype(str).unique():
        labels.setdefault(slug, slug.replace("-", " ").title())
    if "bangalore" in labels:
        labels["bangalore"] = "Bengaluru"
    return labels


def available_market_labels(db) -> tuple[dict[str, str], bool]:
    """Use live market coverage when available, otherwise the seeded demo markets."""
    try:
        markets = live_market_labels()
        if markets:
            return markets, True
    except Exception:
        pass
    demo_cities = db.scalars(select(Locality.city).distinct().order_by(Locality.city)).all()
    slug_overrides = {"bengaluru": "bangalore", "bangalore": "bangalore", "delhi": "delhi"}
    markets = {
        slug_overrides.get(str(city).casefold(), re.sub(r"[^a-z0-9]+", "-", str(city).casefold()).strip("-")): str(city)
        for city in demo_cities
    }
    return markets, False


def postgres_source_diagnostic() -> str:
    """Describe source configuration without revealing a hostname or credentials."""
    configured_url = ""
    try:
        configured_url = str(st.secrets.get("PROPCLUE_SOURCE_DATABASE_URL", "")).strip()
    except Exception:
        pass
    configured_url = configured_url or os.environ.get("PROPCLUE_SOURCE_DATABASE_URL", "").strip()
    if not configured_url:
        return "PROPCLUE_SOURCE_DATABASE_URL is not set; the app is using its localhost default, which Cloud cannot reach."
    try:
        host = (make_url(configured_url).host or "").casefold()
    except Exception:
        return "PROPCLUE_SOURCE_DATABASE_URL is set, but it is not a valid SQLAlchemy database URL."
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "PROPCLUE_SOURCE_DATABASE_URL points to localhost; Cloud cannot reach a database on your PC."
    return "PROPCLUE_SOURCE_DATABASE_URL is set to a remote host (hostname hidden)."


def safe_connection_error(exc: Exception) -> str:
    """Return useful driver detail while removing embedded connection strings."""
    message = str(exc)
    message = re.sub(
        r"(?i)(?:postgres(?:ql)?(?:\+[-\w]+)?|mongodb(?:\+srv)?):\/\/[^\s\"']+",
        "[connection URL redacted]",
        message,
    )
    return message[:400] or type(exc).__name__


def login_screen(db):
    brand_lockup()
    st.markdown("""
    <style>
    .pc-home-hero{position:relative;overflow:hidden;min-height:390px;padding:2.2rem 2rem;border:1px solid #dce8ed;border-radius:28px;background:radial-gradient(ellipse at 77% 25%,rgba(28,183,155,.18),transparent 34%),linear-gradient(120deg,#071c2b 0%,#0b2639 56%,#123a49 100%);box-shadow:0 22px 54px rgba(7,30,48,.15);color:#f4f9fc}
    .pc-home-hero:after{content:"";position:absolute;inset:0;background-image:linear-gradient(rgba(180,220,229,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(180,220,229,.055) 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(90deg,transparent 26%,#000 100%);pointer-events:none}
    .pc-home-copy{position:relative;z-index:2;max-width:610px}.pc-home-kicker{display:inline-flex;align-items:center;gap:.5rem;border:1px solid rgba(95,227,179,.3);border-radius:99px;padding:.42rem .72rem;color:#9ce8d1;background:rgba(12,93,83,.24);font-size:.69rem;font-weight:800;letter-spacing:.13em;text-transform:uppercase}
    .pc-home-title{margin:1.2rem 0 .8rem;color:#f6fbfd;font-size:clamp(2.1rem,4vw,3.45rem);line-height:1.04;letter-spacing:-.055em;font-weight:780}.pc-home-title span{color:#61dfc4}.pc-home-subtitle{max-width:530px;color:#b6cbd5;font-size:1.04rem;line-height:1.65}
    .pc-home-signals{display:flex;flex-wrap:wrap;gap:.55rem;margin-top:1.4rem}.pc-home-signal{padding:.48rem .68rem;border:1px solid rgba(194,222,231,.15);border-radius:12px;background:rgba(255,255,255,.055);color:#d6e4e9;font-size:.78rem}
    .pc-home-map{position:absolute;right:2.2rem;top:2.2rem;width:37%;height:78%;min-width:255px;border:1px solid rgba(172,224,225,.22);border-radius:22px;background:linear-gradient(145deg,rgba(12,44,58,.78),rgba(9,34,47,.9));box-shadow:0 20px 40px rgba(0,0,0,.18);transform:rotate(1.2deg);z-index:1;overflow:hidden}
    .pc-home-street{position:absolute;height:1px;background:rgba(202,225,222,.22);transform-origin:left}.pc-home-heat{position:absolute;border-radius:50%;filter:blur(16px);opacity:.78}.pc-home-pin{position:absolute;width:12px;height:12px;border:3px solid #f4fffc;border-radius:50%;background:#17b89b;box-shadow:0 0 0 7px rgba(23,184,155,.19),0 0 25px rgba(23,184,155,.7)}
    .pc-home-map-label{position:absolute;left:1rem;top:1rem;padding:.58rem .7rem;border:1px solid rgba(206,230,230,.16);border-radius:12px;background:rgba(5,24,36,.72);backdrop-filter:blur(12px)}.pc-home-map-label small{display:block;color:#81a7b5;font-size:.61rem;letter-spacing:.12em;font-weight:800}.pc-home-map-label strong{display:block;color:#eef8f6;font-size:.87rem;margin-top:.2rem}.pc-home-map-score{position:absolute;right:.8rem;bottom:.8rem;padding:.68rem .78rem;border:1px solid rgba(197,226,223,.18);border-radius:14px;background:rgba(5,24,36,.77);backdrop-filter:blur(12px)}.pc-home-map-score small{display:block;color:#92b2bc;font-size:.6rem;letter-spacing:.1em}.pc-home-map-score strong{color:#fff;font-size:1.3rem}.pc-home-map-score span{color:#72e2c4;font-size:.7rem}
    .pc-home-step{color:#315168;font-size:.83rem}.pc-home-step strong{display:block;color:#102a43;font-size:.96rem;margin-bottom:.24rem}
    @media(max-width:800px){.pc-home-hero{padding:1.4rem;min-height:565px}.pc-home-copy{max-width:100%}.pc-home-map{top:auto;right:1.1rem;bottom:1rem;width:calc(100% - 2.2rem);height:205px;min-width:0;transform:none}.pc-home-title{font-size:2.2rem}}
    </style>
    <section class="pc-home-hero">
      <div class="pc-home-copy">
        <div class="pc-home-kicker">⌖ Real estate intelligence · Buyer workspace</div>
        <h1 class="pc-home-title">Find the right <span>locality</span><br>before the right property.</h1>
        <div class="pc-home-subtitle">Explore market signals, compare live project records and understand the area around a property before you shortlist.</div>
        <div class="pc-home-signals"><span class="pc-home-signal">Locality intelligence</span><span class="pc-home-signal">Project search</span><span class="pc-home-signal">Radius exploration</span><span class="pc-home-signal">Evidence-aware Q&amp;A</span></div>
      </div>
      <div class="pc-home-map" aria-label="Illustrative market heatmap visual">
        <div class="pc-home-street" style="left:-10%;top:36%;width:125%;transform:rotate(24deg)"></div><div class="pc-home-street" style="left:-5%;top:70%;width:120%;transform:rotate(-22deg)"></div><div class="pc-home-street" style="left:48%;top:-5%;width:115%;transform:rotate(72deg)"></div><div class="pc-home-street" style="left:19%;top:3%;width:110%;transform:rotate(103deg)"></div>
        <div class="pc-home-heat" style="left:17%;top:30%;width:105px;height:105px;background:#0ea58d"></div><div class="pc-home-heat" style="left:45%;top:46%;width:120px;height:120px;background:#f0a63b"></div><div class="pc-home-heat" style="left:66%;top:20%;width:90px;height:90px;background:#31a7a0"></div><div class="pc-home-heat" style="left:22%;top:68%;width:85px;height:85px;background:#d77355"></div>
        <div class="pc-home-pin" style="left:53%;top:48%"></div><div class="pc-home-map-label"><small>ILLUSTRATIVE HEATMAP</small><strong>Locality signals</strong></div><div class="pc-home-map-score"><small>SPATIAL PREVIEW</small><strong>Explore</strong><span>conceptual · live view after sign-in</span></div>
      </div>
    </section>
    """, unsafe_allow_html=True)
    left, right = st.columns([1.0, 1.0], gap="large")
    with left:
        st.markdown("#### A clearer property decision, from area to project")
        steps = st.columns(3)
        steps[0].markdown("<div class='pc-home-step'><strong>01 · Explore</strong>Read locality and market signals.</div>", unsafe_allow_html=True)
        steps[1].markdown("<div class='pc-home-step'><strong>02 · Focus</strong>Search a locality or project on the map.</div>", unsafe_allow_html=True)
        steps[2].markdown("<div class='pc-home-step'><strong>03 · Decide</strong>Compare evidence and save your shortlist.</div>", unsafe_allow_html=True)
        st.caption("Source records may be estimated or unverified. The demo does not independently confirm legal, title, tax, bank, price, or availability claims.")
    with right:
        with st.container(border=True):
            st.subheader("Sign in or create a demo account")
            st.caption("Demo sign-in accepts any password. Buyer and builder workflow records remain synthetic.")
            mode = st.segmented_control("Account access", ["Sign in", "Create account"], default="Sign in", key="login-mode")
            account_type = st.selectbox("Account type", ["Buyer", "Builder", "Channel partner"], key="login_account_type")
            markets, has_live_market_data = available_market_labels(db)
            if not has_live_market_data:
                st.info("Live PropClue market data is unreachable right now. Showing demo market choices until the source database is available.")
            if markets:
                market_slugs = sorted(markets, key=lambda slug: markets[slug].casefold())
                city_labels = {"Bengaluru" if slug == "bangalore" else markets[slug]: slug for slug in market_slugs}
                city_options = list(city_labels)
                preferred_city = "Bengaluru" if "Bengaluru" in city_labels else city_options[0]
                city = st.selectbox("Market", city_options, index=city_options.index(preferred_city), key="login_market")
                selected_market_slug = city_labels[city]
            else:
                st.warning("Live market options could not be loaded. You can still sign in to the demo.")
                city = st.selectbox("Market", ["Bengaluru", "Delhi"], key="login_market-fallback")
                selected_market_slug = "bangalore" if city == "Bengaluru" else "delhi"
            builders = db.scalars(select(Builder).order_by(Builder.name)).all()
            builder_by_name = {f"{x.name} · {x.city}": x for x in builders}
            eligible_builders = {label: builder for label, builder in builder_by_name.items() if builder.city == city}
            if account_type in {"Builder", "Channel partner"} and not eligible_builders:
                st.info("This demo has no builder workspace linked to the selected live market. Choose Bengaluru or Delhi for a builder or channel-partner demo login.")
            with st.form("demo_login_form", border=False):
                username = st.text_input("Username or email", placeholder="Enter a demo email")
                _password = st.text_input("Password", type="password", placeholder="Demo only; not validated")
                selected_builder = None
                if account_type in {"Builder", "Channel partner"} and eligible_builders:
                    selected_builder = st.selectbox("Builder company / requested partner affiliation", list(eligible_builders))
                partner_company = st.text_input("Channel partner company", max_chars=120) if account_type == "Channel partner" else ""
                record_interactions = st.checkbox("Record my demo interactions in this session", value=True,
                                                  help="Hover and explore actions are stored as synthetic demo activity.")
                action_label = "Create demo account" if mode == "Create account" else "Continue to demo"
                submit = st.form_submit_button(action_label,
                                               type="primary", width="stretch",
                                               disabled=account_type in {"Builder", "Channel partner"} and not eligible_builders)
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
                st.session_state.market_slug = selected_market_slug
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
    st.caption("Live listing/locality records are read from PropClue databases; workflow scenarios and demo analytics are synthetic.")


def get_city_filter(db, user: User) -> str:
    market_labels, has_live_market_data = available_market_labels(db)
    if not has_live_market_data:
        st.warning("Live PropClue source is unavailable. Showing demo markets; live listings will return when the source database is reachable.")
    markets = sorted(market_labels, key=lambda slug: market_labels[slug].casefold())
    if not markets:
        st.warning("No published markets were found in the PropClue source database.")
        st.session_state["market_slug"] = ""
        return user.city

    preferred = st.session_state.get("market_slug")
    if preferred not in markets:
        requested_city = str(st.session_state.get("city_filter", user.city)).casefold()
        preferred = next((slug for slug in markets if market_labels[slug].casefold() == requested_city), None)
        if preferred is None and requested_city in {"bengaluru", "bangalore"}:
            preferred = next((slug for slug in markets if slug == "bangalore"), None)
        if preferred is None:
            preferred = "bangalore" if "bangalore" in markets else markets[0]
    selected_slug = st.selectbox(
        "Market", markets, index=markets.index(preferred),
        format_func=lambda slug: market_labels[slug], key="live-market-selector",
    )
    st.session_state["market_slug"] = selected_slug
    selected_city = market_labels[selected_slug]
    # Preserve compatibility for the still-labelled Bengaluru demo-only views.
    st.session_state.city_filter = "Bengaluru" if selected_slug == "bangalore" else selected_city
    return st.session_state.city_filter


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
        top_header("Discover projects", "Search active project listings from the selected live PropClue market.")
    show_source_catalog(st.session_state.get("market_slug"))

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


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two stored locality centroids."""
    from math import asin, cos, radians, sin, sqrt
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    value = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(value))


def property_map_points(listings: pd.DataFrame, localities: pd.DataFrame) -> pd.DataFrame:
    """Return visible, deterministic listing circles around matched locality centroids.

    The source catalogue does not contain project coordinates. These small offsets are
    deliberately marked approximate and must never be treated as property addresses.
    """
    from math import cos, radians, sin

    coords = {
        str(row.slug): (float(row.latitude), float(row.longitude), str(row.name))
        for row in localities.dropna(subset=["latitude", "longitude"]).itertuples(index=False)
    }
    colors = {
        "residential": [20, 184, 166, 225],
        "mixed_use": [249, 115, 22, 235],
        "commercial": [37, 99, 235, 230],
    }
    records = []
    for row in listings.itertuples(index=False):
        slug = getattr(row, "locality_match_slug", None)
        if not slug or str(slug) not in coords:
            continue
        lat, lon, locality_name = coords[str(slug)]
        digest = hashlib.sha256(f"listing-map:{getattr(row, 'id', row.project_name)}".encode()).hexdigest()
        angle = int(digest[:8], 16) / 0xFFFFFFFF * 6.283185307179586
        offset_km = 0.08 + (int(digest[8:12], 16) % 44) / 100
        point_lat = lat + (offset_km * cos(angle)) / 111.32
        point_lon = lon + (offset_km * sin(angle)) / max(111.32 * cos(radians(lat)), 1)
        project_name = str(getattr(row, "project_name", "Project listing"))
        builder = str(getattr(row, "builder_name", None) or "Builder not provided")
        project_type = str(getattr(row, "project_type", "") or "").casefold().replace(" ", "_")
        minimum, maximum = getattr(row, "price_min", None), getattr(row, "price_max", None)
        price = "–".join(money(value, True) for value in (minimum, maximum) if pd.notna(value)) or "Not listed"
        status = str(getattr(row, "construction_status", None) or "Not provided").replace("_", " ").title()
        rera = str(getattr(row, "rera_number", None) or "Not provided")
        building_age = 1 + (int(digest[12:16], 16) % 35)
        detail = (
            f"<b>{html.escape(project_name)}</b><br>Builder: {html.escape(builder)}"
            f"<br>Locality: {html.escape(locality_name)}<br>Listed range: {html.escape(price)}"
            f"<br>Stage: {html.escape(status)}<br>RERA reference: {html.escape(rera)}"
            f"<br>Bank approval: not verified<br>Building age: {building_age} years · intelligence_demo"
            "<br>Marker is an approximate locality-offset demo coordinate"
        )
        records.append({
            "listing_id": int(getattr(row, "id", 0)), "latitude": point_lat, "longitude": point_lon, "name": project_name,
            "detail": detail, "radius_px": 20,
            "fill_color": colors.get(project_type, [99, 102, 241, 220]),
            "locality": locality_name, "building_age_demo": building_age,
            "price_range": price, "project_type": project_type,
        })
    return pd.DataFrame(records)


def intelligence_demo_pois(latitude: float, longitude: float, radius_km: float, area_slug: str) -> pd.DataFrame:
    """Create clearly synthetic POI stand-ins for radius-map UX development."""
    from math import cos, radians, sin

    catalog = [
        ("School", "School campus"), ("Hospital", "Hospital cluster"),
        ("Tech park", "Business / technology campus"), ("Petrol bunk", "Fuel station"),
        ("Airport", "Airport access point"), ("Railway station", "Rail station"),
        ("Mall", "Retail and mall hub"), ("Public transport", "Metro / bus interchange"),
    ]
    colors = {
        "School": [59, 130, 246, 230], "Hospital": [239, 68, 68, 235],
        "Tech park": [139, 92, 246, 235], "Petrol bunk": [245, 158, 11, 235],
        "Airport": [15, 118, 110, 235], "Railway station": [217, 70, 239, 235],
        "Mall": [236, 72, 153, 235], "Public transport": [14, 165, 233, 235],
    }
    radius = max(0.5, float(radius_km))
    records = []
    for index, (category, label) in enumerate(catalog):
        digest = hashlib.sha256(f"{area_slug}:intelligence_demo:{category}".encode()).hexdigest()
        fraction = 0.14 + (int(digest[:8], 16) % 7400) / 10000
        distance = min(radius * fraction, radius * 0.96)
        bearing = int(digest[8:16], 16) / 0xFFFFFFFF * 6.283185307179586
        lat = latitude + (distance * cos(bearing)) / 111.32
        lon = longitude + (distance * sin(bearing)) / max(111.32 * cos(radians(latitude)), 1)
        name = f"{label} {index + 1:02d} · intelligence_demo"
        records.append({
            "latitude": lat, "longitude": lon, "name": name, "category": category,
            "distance_km": round(distance, 1), "radius_px": 10, "fill_color": colors[category],
            "detail": (
                f"<b>{html.escape(category)}</b><br>{html.escape(name)}"
                f"<br>{distance:.1f} km straight-line from {html.escape(area_slug)}"
                "<br>Source: synthetic intelligence_demo · not a real venue or route"
            ),
            "source": "intelligence_demo · synthetic",
        })
    return pd.DataFrame(records).sort_values("distance_km").reset_index(drop=True)


def render_circle_map(
    center_lat: float, center_lon: float, zoom: float, radius_km: float,
    localities: pd.DataFrame | None = None, projects: pd.DataFrame | None = None,
    pois: pd.DataFrame | None = None, height: int = 500, alt: str = "Property and locality map",
    show_heatmap: bool = True,
) -> None:
    """Render stable pixel-sized circles with hover cards, plus a radius boundary."""
    layers = []
    if show_heatmap and localities is not None and not localities.empty and "heat_weight" in localities.columns:
        layers.append(pdk.Layer(
            "HeatmapLayer", data=localities, get_position="[longitude, latitude]",
            get_weight="heat_weight", radius_pixels=82, intensity=1.0, threshold=0.04,
            opacity=0.48, pickable=False,
            color_range=[[10, 110, 110, 0], [13, 148, 136, 90], [245, 158, 11, 155], [239, 68, 68, 215]],
        ))
    radius_frame = pd.DataFrame([{"latitude": center_lat, "longitude": center_lon, "radius_m": radius_km * 1000}])
    layers.append(pdk.Layer(
        "ScatterplotLayer", data=radius_frame, get_position="[longitude, latitude]",
        get_radius="radius_m", radius_units="meters", stroked=True, filled=False,
        get_line_color=[13, 148, 136, 155], line_width_min_pixels=2,
        pickable=False, auto_highlight=False,
    ))
    if localities is not None and not localities.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=localities, get_position="[longitude, latitude]",
            get_radius=9, radius_units="pixels", radius_min_pixels=8, radius_max_pixels=13,
            get_fill_color="fill_color", stroked=True, get_line_color=[255, 255, 255, 235],
            line_width_min_pixels=1, pickable=True, auto_highlight=True,
        ))
    if projects is not None and not projects.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=projects, get_position="[longitude, latitude]",
            get_radius="radius_px", radius_units="pixels", radius_min_pixels=16, radius_max_pixels=24,
            get_fill_color="fill_color", stroked=True, get_line_color=[255, 255, 255, 245],
            line_width_min_pixels=2, pickable=True, auto_highlight=True,
        ))
    if pois is not None and not pois.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=pois, get_position="[longitude, latitude]",
            get_radius="radius_px", radius_units="pixels", radius_min_pixels=9, radius_max_pixels=13,
            get_fill_color="fill_color", stroked=True, get_line_color=[255, 255, 255, 255],
            line_width_min_pixels=2, pickable=True, auto_highlight=True,
        ))
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0),
        tooltip={"html": "{detail}", "style": {"backgroundColor": "#0c213c", "color": "#f5f8fc", "fontSize": "12px", "padding": "10px", "maxWidth": "330px"}},
        map_style="light",
    )
    st.pydeck_chart(deck, width="stretch", height=height, alt=alt)


def demo_property_rating(
    lifestyle_score: object, commute_score: object, building_age: int, pois: pd.DataFrame,
) -> tuple[float, pd.DataFrame]:
    """Explainable 10-point demonstration rating; proximity features are simulated."""
    def score_10(value: object, fallback: float) -> float:
        try:
            if pd.notna(value):
                numeric = float(value)
                return max(0.0, min(10.0, numeric / 10 if numeric > 10 else numeric))
        except (TypeError, ValueError):
            pass
        return fallback

    lifestyle = score_10(lifestyle_score, 6.5)
    commute = score_10(commute_score, 6.2)
    age_score = max(2.5, min(9.5, 10 - max(0, building_age - 3) * 0.22))
    mean_distance = float(pd.to_numeric(pois.get("distance_km", pd.Series(dtype=float)), errors="coerce").dropna().mean()) if not pois.empty else 12.0
    poi_avg = max(2.0, min(10.0, 9.0 - mean_distance * 0.22))
    components = [
        ("School, hospital, tech park, fuel, airport, rail, mall & transit proximity", poi_avg, 35, "Synthetic intelligence_demo POI distances"),
        ("Lifestyle score", lifestyle, 20, "PostgreSQL locality score" if lifestyle_score is not None and pd.notna(lifestyle_score) else "Demo fallback"),
        ("Commute score", commute, 20, "Connectivity score used as a commute proxy"),
        ("Representative building age", age_score, 15, f"Synthetic {building_age}-year age · intelligence_demo"),
        ("Builder construction warranty", 5.0, 10, "Warranty not connected; neutral placeholder, no builder warranty claim"),
    ]
    rating = round(sum(score * weight for _, score, weight, _ in components) / sum(weight for _, _, weight, _ in components), 1)
    table = pd.DataFrame([
        {"Signal": label, "Score / 10": f"{score:.1f}", "Weight": f"{weight}%", "Evidence": evidence}
        for label, score, weight, evidence in components
    ])
    return rating, table


def render_area_project_brief(
    name: str, lifestyle_score: object, commute_score: object, building_age: int,
    pois: pd.DataFrame, scope_label: str = "Area / Project Brief",
) -> float:
    rating, components = demo_property_rating(lifestyle_score, commute_score, building_age, pois)
    with st.container(border=True):
        st.markdown(f"#### {scope_label} · {name}")
        cols = st.columns(5)
        cols[0].metric("Demo property rating", f"{rating:.1f}/10", border=True, help="Weighted prototype score using the breakdown below; it is not an investment, safety, or legal rating.")
        cols[1].metric("Lifestyle score", f"{float(lifestyle_score):.1f}/10" if lifestyle_score is not None and pd.notna(lifestyle_score) else "Demo 6.5/10", border=True)
        cols[2].metric("Commute score", f"{float(commute_score):.1f}/10" if commute_score is not None and pd.notna(commute_score) else "Proxy 6.2/10", border=True, help="Connectivity score is used as a commute proxy; no route-time service is connected.")
        cols[3].metric("Representative building age", f"{building_age} years · demo", border=True)
        cols[4].metric("Builder warranty", "Not verified", border=True, help="No construction-quality warranty document or builder warranty feed is connected.")
        if not pois.empty:
            poi_view = pois[["category", "name", "distance_km", "source"]].rename(columns={
                "category": "Nearby category", "name": "Nearest demo point", "distance_km": "Straight-line km", "source": "Data label",
            })
            st.dataframe(poi_view, width="stretch", hide_index=True, alt=f"Nearby amenity and transit demo points around {name}")
            st.caption("Hover the colored map circles for category and distance. Nearby place names and coordinates are synthetic `intelligence_demo` stand-ins, not real venues or routes.")
        st.dataframe(components, width="stretch", hide_index=True, alt=f"Explainable demo property rating breakdown for {name}")
        st.caption("Rating method: weighted mean out of 10. Proximity 35%, lifestyle 20%, commute proxy 20%, building age 15%, builder warranty 10%. Missing warranty uses a neutral placeholder and remains unverified; proximity, representative building age and fallback scores are synthetic.")
    return rating


def render_property_legal_and_returns(project_name: str, price_min: object, price_max: object, rental_yield: object = None) -> None:
    """Show per-property verification coverage and clearly illustrative return horizons."""
    with st.container(border=True):
        st.markdown("#### Legal, title & bank checks")
        st.caption(f"Property: **{project_name}** · status reflects connected evidence only.")
        legal_rows = [
            ("Title chain / encumbrance certificate", "Not connected"),
            ("Khata / tax receipts / technical clearance", "Not connected"),
            ("RERA reference", "Reference presence only; not verified"),
            ("Bank approval / bank letter", "Not verified; no letter attached"),
        ]
        st.dataframe(pd.DataFrame(legal_rows, columns=["Check", "Status"]), width="stretch", hide_index=True, alt=f"Legal and bank evidence status for {project_name}")
        with st.expander("Legal support contact"):
            st.write("**Assigned lawyer:** Not onboarded")
            st.write("**Practice focus:** Property title, encumbrance, Khata and registration review")
            st.write("**Bar Council enrollment, jurisdiction and contact:** Not provided or verified")
            st.caption("A lawyer directory is not connected. PropClue will show a named counsel only after identity and credentials are verified; this screen does not invent a real lawyer profile.")
        st.info("PropClue supports the buyer journey through ownership/registration confirmation. This demo does not verify legal completion, bank sanction or registration; confirm those with the issuing authority and your lawyer.", title="Support through ownership")

    try:
        low = float(price_min) if pd.notna(price_min) else None
        high = float(price_max) if pd.notna(price_max) else None
    except (TypeError, ValueError):
        low = high = None
    if low is None and high is None:
        st.info("Investment horizons are unavailable because this property has no asking-price value in the connected source.")
        return
    current_price = ((low if low is not None else high) + (high if high is not None else low)) / 2
    yield_default = 3.0
    try:
        if rental_yield is not None and pd.notna(rental_yield):
            yield_default = max(0.0, min(12.0, float(rental_yield)))
    except (TypeError, ValueError):
        pass
    st.markdown("#### Rental income & investment outlook · scenario model")
    st.caption("Illustrative gross scenario, not a forecast or investment advice. Rent yield and annual price growth are editable assumptions; excludes vacancy, taxes, fees, financing, maintenance and sale costs.")
    inputs = st.columns(2)
    rent_yield = inputs[0].slider("Gross rent yield assumption (%)", 0.0, 12.0, float(round(yield_default, 1)), 0.1, key=f"return-yield-{hashlib.sha1(project_name.encode()).hexdigest()[:8]}")
    price_growth = inputs[1].slider("Annual property value change assumption (%)", -5.0, 15.0, 5.0, 0.5, key=f"return-growth-{hashlib.sha1(project_name.encode()).hexdigest()[:8]}")
    rows = []
    for years in (1, 3, 5):
        estimated_value = current_price * (1 + price_growth / 100) ** years
        gross_rent = current_price * rent_yield / 100 * years
        gross_total = estimated_value + gross_rent
        total_return = (gross_total / current_price - 1) * 100
        rows.append({
            "Horizon": f"{years} year{'s' if years > 1 else ''}",
            "Indicative value": money(estimated_value, True),
            "Cumulative gross rent": money(gross_rent, True),
            "Illustrative gross return": f"{total_return:+.1f}%",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, alt=f"Illustrative 1, 3 and 5 year return scenarios for {project_name}")
    st.caption("Scenario uses the midpoint of the stored asking range. If a rental yield is stored in MongoDB, it supplies the starting assumption; otherwise the demo starts at 3%. Stored inputs may be estimates and are not independently verified.")


def make_hsr_zoomin_demo(center_lat: float, center_lon: float) -> pd.DataFrame:
    """Deterministic illustrative parcel/building shapes; never treated as source GIS."""
    from math import cos, radians
    rows, cols = 4, 5
    lat_step = 0.00082
    lon_step = lat_step / max(cos(radians(center_lat)), 0.1)
    lat_half, lon_half = 0.00031, lon_step * 0.36
    records = []
    land_uses = ["Residential", "Mixed use", "Residential", "Commercial"]
    for index in range(rows * cols):
        row, col = divmod(index, cols)
        lat = center_lat + (row - (rows - 1) / 2) * lat_step
        lon = center_lon + (col - (cols - 1) / 2) * lon_step
        parcel = [
            [lon - lon_half, lat - lat_half], [lon + lon_half, lat - lat_half],
            [lon + lon_half, lat + lat_half], [lon - lon_half, lat + lat_half],
        ]
        building_half_lat, building_half_lon = lat_half * 0.58, lon_half * 0.58
        building = [
            [lon - building_half_lon, lat - building_half_lat],
            [lon + building_half_lon, lat - building_half_lat],
            [lon + building_half_lon, lat + building_half_lat],
            [lon - building_half_lon, lat + building_half_lat],
        ]
        asking_psf = 8500 + ((index * 733) % 9500)
        available_units = 18 + ((index * 17) % 74)
        absorption_pct = round(2.5 + ((index * 13) % 77) / 10, 1)
        records.append({
            "parcel_id": f"HSR-P{index + 1:02d}-zoomedin_demo",
            "building_id": f"HSR-B{index + 1:02d}-zoomedin_demo",
            "name": f"Parcel {index + 1:02d} · zoomedin_demo",
            "building_name": f"Building {index + 1:02d} · zoomedin_demo",
            "land_use": land_uses[index % len(land_uses)],
            "floors": 4 + ((index * 7) % 25),
            "units": 32 + ((index * 19) % 180),
            "available_units": available_units,
            "asking_psf": asking_psf,
            "absorption_pct": absorption_pct,
            "yoy_pct": round(-2 + ((index * 11) % 180) / 10, 1),
            "polygon": parcel,
            "building_polygon": building,
            "fill_color": [20, 184, 166, 105] if land_uses[index % len(land_uses)] == "Residential" else ([249, 115, 22, 105] if land_uses[index % len(land_uses)] == "Mixed use" else [37, 99, 235, 105]),
            "latitude": lat,
            "longitude": lon,
            "distance_display": "Synthetic HSR Layout mock geometry",
            "matching_listings": "synthetic",
            "composite_score": "synthetic",
            "detail": f"{land_uses[index % len(land_uses)]} · ₹{asking_psf:,}/sq ft · {available_units} sample units",
        })
    return pd.DataFrame(records)


def hsr_demo_figure(parcels: pd.DataFrame, center_lat: float, center_lon: float,
                    selected_parcel_id: str | None = None) -> go.Figure:
    """Local-plane polygon map that does not depend on external basemap tiles."""
    from math import cos, radians
    lon_scale = 111_320 * cos(radians(center_lat))
    lat_scale = 111_320
    palette = {"Residential": "#14b8a6", "Mixed use": "#f97316", "Commercial": "#2563eb"}
    fig = go.Figure()
    for row in parcels.itertuples(index=False):
        x = [(point[0] - center_lon) * lon_scale for point in row.polygon]
        y = [(point[1] - center_lat) * lat_scale for point in row.polygon]
        x.append(x[0]); y.append(y[0])
        selected = row.parcel_id == selected_parcel_id
        hover = (f"<b>{row.name}</b><br>{row.land_use}<br>₹{row.asking_psf:,.0f}/sq ft"
                 f"<br>{row.units} sample homes · {row.floors} floors"
                 f"<br>{row.absorption_pct:.1f}% monthly absorption · {row.yoy_pct:+.1f}% YoY"
                 "<br>All values are synthetic · zoomedin_demo")
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", fill="toself",
            fillcolor=palette.get(row.land_use, "#99f6e4"),
            line={"color":"#f59e0b" if selected else "#134e4a", "width":3 if selected else 1},
            name=row.name, text=[hover] * len(x), hovertemplate="%{text}<extra></extra>",
            showlegend=False))
        bx = [(point[0] - center_lon) * lon_scale for point in row.building_polygon]
        by = [(point[1] - center_lat) * lat_scale for point in row.building_polygon]
        bx.append(bx[0]); by.append(by[0])
        fig.add_trace(go.Scatter(x=bx, y=by, mode="lines", fill="toself",
            fillcolor="rgba(245,158,11,.72)", line={"color":"#fff7ed", "width":1},
            name=row.building_name, text=[hover] * len(bx), hovertemplate="%{text}<extra></extra>",
            showlegend=False))
    for label, color in palette.items():
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", marker={"size":10, "color":color},
                                 name=label, hoverinfo="skip"))
    fig.update_layout(title="HSR Layout building blocks · zoomedin_demo",
        height=560, margin={"l":20,"r":20,"t":55,"b":20},
        paper_bgcolor="white", plot_bgcolor="#f8fafc", hovermode="closest",
        legend={"orientation":"h","y":1.08,"x":0}, dragmode="zoom")
    fig.update_xaxes(title="Meters east / west of HSR centre", showgrid=True, gridcolor="#e2e8f0", zeroline=False)
    fig.update_yaxes(title="Meters north / south of HSR centre", scaleanchor="x", scaleratio=1,
                     showgrid=True, gridcolor="#e2e8f0", zeroline=False)
    return fig


def show_area_agent_report(db, user: User, area: pd.Series, doc: dict,
                           active_area_listings: pd.DataFrame) -> None:
    """Explain source signals, trace an illustrative shock, and gate action on review."""
    market = doc.get("real_estate", {}) if isinstance(doc, dict) else {}
    people = doc.get("demography", {}) if isinstance(doc, dict) else {}
    infra = doc.get("infrastructure", {}) if isinstance(doc, dict) else {}
    future = doc.get("future_potential", {}) if isinstance(doc, dict) else {}
    connectivity = doc.get("connectivity", {}) if isinstance(doc, dict) else {}
    sectors = doc.get("sectors", []) if isinstance(doc, dict) else []
    sector_data = next((s.get("raw_data", {}) for s in sectors if isinstance(s, dict) and s.get("slug") == "it_tech"), {})
    meta = doc.get("meta", {}) if isinstance(doc, dict) else {}
    quality = str(meta.get("data_quality", "not specified"))
    sources = meta.get("data_sources", [])
    synthetic_source = any("ai_generated" in str(x).casefold() for x in sources)
    pg_listings = len(active_area_listings)
    mongo_listings = market.get("active_listings_count") if isinstance(market, dict) else None
    two_bed_rent = market.get("avg_rent_2bhk_inr") if isinstance(market, dict) else None
    monthly_income = people.get("avg_monthly_household_income_inr") if isinstance(people, dict) else None
    rent_burden = (float(two_bed_rent) / float(monthly_income) * 100) if two_bed_rent and monthly_income else None

    st.markdown(f"### Area intelligence · {area['name']}")
    st.caption(f"Supervisor report · source quality: {quality} · source tags: {', '.join(map(str, sources)) if sources else 'not specified'} · generated scenario outputs use `intelligence_demo`.")
    agent_cols = st.columns(4, gap="small")
    with agent_cols[0].container(border=True):
        st.markdown("**Market agent**")
        psf = market.get("avg_sale_psf_apartment") if isinstance(market, dict) else None
        st.metric("Apartment ask", f"₹{float(psf):,.0f}/sq ft" if psf else "Not stored")
        trend = market.get("price_trend_pct_6m") if isinstance(market, dict) else None
        st.caption(f"6-month trend {float(trend):+.1f}% · MongoDB" if trend is not None else "6-month trend not stored")
    with agent_cols[1].container(border=True):
        st.markdown("**Demand agent**")
        st.metric("Renter share", f"{people['renter_ratio_pct']}%" if isinstance(people, dict) and people.get("renter_ratio_pct") is not None else "Not stored")
        st.caption(f"2BHK rent / household income: {rent_burden:.1f}% · intelligence_demo" if rent_burden is not None else "Rent burden cannot be calculated from stored fields")
    with agent_cols[2].container(border=True):
        st.markdown("**Supply & access agent**")
        st.metric("PostgreSQL matched projects", f"{pg_listings:,}")
        if mongo_listings is not None:
            st.caption(f"MongoDB area count: {int(mongo_listings):,} · cross-database field, not confirmed inventory")
        st.caption(f"Nearest metro stored at {connectivity.get('nearest_metro_distance_km')} km" if isinstance(connectivity, dict) and connectivity.get("nearest_metro_distance_km") is not None else "No metro distance stored")
    with agent_cols[3].container(border=True):
        st.markdown("**Data quality agent**")
        if mongo_listings is not None and int(mongo_listings) != pg_listings:
            st.warning(f"Catalogue mismatch: MongoDB area count {int(mongo_listings):,} vs {pg_listings:,} exact PostgreSQL matches.")
        else:
            st.write("No active-listing count mismatch detected in the selected fields.")
        st.caption("AI-generated area fields need source verification." if synthetic_source else "Confirm freshness and provenance before decisions.")

    st.markdown("#### Trace a shock through this locality · intelligence_demo")
    scenarios = ["Baseline area signals", "Aluminium input cost +10%", "Home-loan rate +0.5 percentage points", "Metro opening delayed 12 months", "IT employment -5%"]
    scenario = st.selectbox("Scenario", scenarios, key=f"area-agent-scenario-{area['slug']}")
    chain = []
    formula = "This baseline reports stored area indicators; it does not predict outcomes."
    if scenario == "Aluminium input cost +10%":
        share = st.slider("Assumed aluminium share of build cost (%) · intelligence_demo", 1, 10, 3, key=f"aluminium-share-{area['slug']}")
        build_cost_change = share * 10 / 100
        formula = f"Assumption: aluminium is {share}% of build cost. A 10% aluminium shock implies about +{build_cost_change:.2f}% build-cost pressure (10% × {share}%). It does not imply the same home-price increase."
        chain = ["Aluminium price +10%", f"Build cost +{build_cost_change:.2f}% if share is {share}%", "Builder margin / launch price decision", "Launch timing and new supply", "Buyer affordability, absorption and resale competition"]
    elif scenario == "Home-loan rate +0.5 percentage points":
        principal, years, base_rate = 8_000_000, 20, 8.5
        def emi(rate):
            monthly = rate / 1200
            periods = years * 12
            return principal * monthly * (1 + monthly) ** periods / ((1 + monthly) ** periods - 1)
        base_emi, shock_emi = emi(base_rate), emi(base_rate + .5)
        change = (shock_emi / base_emi - 1) * 100
        formula = f"Illustration only: ₹80 lakh loan, 20 years, assumed 8.5% → 9.0%; monthly EMI rises about {change:.1f}% ({money(base_emi)} → {money(shock_emi)}). No lender rates are connected."
        chain = ["Loan rate +0.5 pp", f"Illustrative EMI +{change:.1f}%", "Buyer budget / loan eligibility", "Enquiry-to-visit and absorption", "Negotiation pressure on higher-ticket homes"]
    elif scenario == "Metro opening delayed 12 months":
        station = connectivity.get("nearest_metro_station", "planned transit link") if isinstance(connectivity, dict) else "planned transit link"
        distance = connectivity.get("nearest_metro_distance_km") if isinstance(connectivity, dict) else None
        formula = f"Stored access: {station}" + (f" · {distance} km" if distance is not None else "") + ". Delay effect is qualitative; a verified project schedule and route-level demand series are not connected."
        chain = ["Metro opening delayed 12 months", "Longer wait for access benefit", "Commuter preference / search radius", "Corridor demand and absorption", "Transit-premium pricing risk"]
    elif scenario == "IT employment -5%":
        workforce_share = sector_data.get("tech_workforce_share_pct") if isinstance(sector_data, dict) else None
        exposed = float(workforce_share) * .05 if workforce_share is not None else None
        formula = (f"Stored IT workforce share {workforce_share}%; a 5% shock implies {exposed:.1f}% of the total-workforce proxy exposed." if exposed is not None else "No locality IT-workforce share is stored; this pathway is qualitative.") + " Both the shock and pathway are illustrative, not employment forecasts."
        chain = ["IT employment -5%", "Household income / job confidence", "Rental demand and upgrade purchases", "Vacancy and absorption", "Rent and resale-price pressure"]
    else:
        chain = ["Observed locality fields", "Buyer demand + renter share", "Matched project supply", "Price / rent / absorption signals", "Supervisor checks source gaps before action"]
    flow = "flowchart LR\n" + "\n".join(f'  N{i}["{label}"]' + (f" --> N{i+1}" if i < len(chain) - 1 else "") for i, label in enumerate(chain))
    st.mermaid_chart(flow, width="stretch", alt=f"Illustrative {scenario} impact pathway for {area['name']}")
    st.caption(formula)
    evidence = pd.DataFrame([{"Path stage": f"{i+1}. {label}", "Evidence tag": "Stored PropClue source" if i == 0 and scenario == "Baseline area signals" else "intelligence_demo assumption"} for i, label in enumerate(chain)])
    st.dataframe(evidence, width="stretch", hide_index=True, height=210, alt="Causal pathway stages and source status for the selected scenario")

    recommendation = "Reconcile MongoDB area counts against the PostgreSQL project catalogue, then verify project availability and price dates before using this area for decisions." if mongo_listings is not None and int(mongo_listings) != pg_listings else "Validate the signal sources and compare verified project availability, prices and documents before acting on this scenario."
    if user.role == "buyer":
        persona = doc.get("gap_analysis", {}).get("b2c_recommendation") if isinstance(doc.get("gap_analysis"), dict) else None
        supervisor_note = persona or "Compare current project availability and total ownership cost with the stored locality signals; independently verify the source and date of each listing."
        title = "Buyer review draft"
    elif user.role == "builder":
        persona = doc.get("gap_analysis", {}).get("builder_recommendation") if isinstance(doc.get("gap_analysis"), dict) else None
        supervisor_note = persona or recommendation
        title = "Builder review draft"
    else:
        supervisor_note = recommendation
        title = "Supervisor review draft"
    with st.container(border=True):
        st.markdown(f"#### Supervisor recommendation · {title}")
        st.write(supervisor_note)
        st.caption("Specialists provide evidence and a scenario pathway; the supervisor drafts a recommendation. Human approval is required before any action. This prototype does not execute campaigns, change prices, or contact anyone.")
        review_note = st.text_input("Human reviewer note (optional)", key=f"area-review-note-{area['slug']}")
        approve_col, revise_col, status_col = st.columns([1, 1, 2])
        if approve_col.button("Approve draft", key=f"approve-area-agent-{area['slug']}", type="primary"):
            st.session_state[f"area_agent_review_{area['slug']}"] = "Approved · demo"
            db.add(AuditLog(actor_id=user.id, action="intelligence_demo_approved", detail=json.dumps({"area":str(area["slug"]),"scenario":scenario,"note":review_note})))
            db.commit()
        if revise_col.button("Request revision", key=f"revise-area-agent-{area['slug']}"):
            st.session_state[f"area_agent_review_{area['slug']}"] = "Revision requested · demo"
            db.add(AuditLog(actor_id=user.id, action="intelligence_demo_revision_requested", detail=json.dumps({"area":str(area["slug"]),"scenario":scenario,"note":review_note})))
            db.commit()
        status_col.caption("Review state: " + st.session_state.get(f"area_agent_review_{area['slug']}", "Pending human review"))


def show_map(db, user: User, city: str, show_header: bool = True):
    """Interactive locality map, isolated HSR building prototype, and area agent review."""
    if show_header:
        top_header("Map intelligence", "Explore a market spatially. Lock an area to see nearby signals, project context and an evidence-aware assistant.")
        st.markdown("""
        <style>
        .pc-map-intro{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem 1.2rem;margin:.25rem 0 1rem;border:1px solid rgba(14,116,109,.17);border-radius:18px;background:linear-gradient(115deg,rgba(13,148,136,.09),rgba(255,255,255,.96) 54%,rgba(245,158,11,.08));box-shadow:0 8px 26px rgba(15,35,58,.05)}
        .pc-map-intro h3{margin:0;color:#102a43;font-size:1.12rem;letter-spacing:-.02em}.pc-map-intro p{margin:.25rem 0 0;color:#60758a;font-size:.88rem}
        .pc-map-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.45rem}.pc-map-badge{border:1px solid #dce8ed;border-radius:999px;background:rgba(255,255,255,.9);padding:.35rem .65rem;color:#38536b;font-size:.76rem;font-weight:700;white-space:nowrap}.pc-map-badge.live{color:#087e70;background:#e9f8f4;border-color:#c8ebe2}.pc-map-badge.demo{color:#9a5a06;background:#fff6e5;border-color:#f5dfae}
        .pc-map-context{padding:.95rem 1.1rem;border-radius:16px;background:#0c213c;color:#f4f8fb;box-shadow:0 14px 35px rgba(12,33,60,.15);margin:.6rem 0 1rem}.pc-map-context small{display:block;color:#a9c4d4;letter-spacing:.08em;text-transform:uppercase;font-weight:700;font-size:.69rem}.pc-map-context strong{display:block;font-size:1.12rem;margin:.15rem 0}.pc-map-context span{color:#cfdae3;font-size:.86rem}
        </style>
        <div class="pc-map-intro"><div><h3>PropClue Spatial Intelligence</h3><p>Search a market, lock a locality, then explore projects and amenity signals around it.</p></div><div class="pc-map-badges"><span class="pc-map-badge live">● Live project catalogue</span><span class="pc-map-badge demo">● Approximate map points</span><span class="pc-map-badge demo">● Demo radius places</span></div></div>
        """, unsafe_allow_html=True)
    market_slug = st.session_state.get("market_slug", "")
    try:
        all_localities, all_listings = load_catalog()
    except Exception as exc:
        st.error("Could not load live PropClue map data from PostgreSQL.", icon=":material/error:")
        st.caption(f"Connection error type: {type(exc).__name__}.")
        return
    localities = all_localities[all_localities["city_slug"] == market_slug].dropna(subset=["latitude", "longitude"]).copy()
    listings = all_listings[all_listings["city_slug"] == market_slug].copy()
    if localities.empty:
        st.info("The selected market has no mapped locality centroids in PostgreSQL.")
        return

    localities = localities.sort_values("name").reset_index(drop=True)
    listing_counts_all = listings["locality_match_slug"].dropna().value_counts().to_dict()
    localities["active_listings"] = localities["slug"].map(listing_counts_all).fillna(0).astype(int)
    locality_slugs = localities["slug"].astype(str).tolist()
    hsr_slug = "bangalore-hsr-layout"
    map_modes = ["Live locality map"]
    if market_slug == "bangalore" and hsr_slug in locality_slugs:
        map_modes.append("HSR building demo · zoomedin_demo")
    map_mode = st.segmented_control("Map layer", map_modes, default=map_modes[0], key="map-layer-mode-v2")
    zoomedin_demo_mode = map_mode == "HSR building demo · zoomedin_demo"
    locked_slug = st.session_state.get("locked_area_slug")
    if locked_slug in set(locality_slugs):
        # Keep the selected locality, map center, radius layer and assistant context in sync.
        st.session_state["live-map-center-v3"] = str(locked_slug)
    preferred_center = max(locality_slugs, key=lambda slug: listing_counts_all.get(slug, 0))
    property_labels: dict[int, str] = {}
    if zoomedin_demo_mode:
        center_slug = hsr_slug
        st.caption("Demo map is centred on HSR Layout automatically. Generated parcels and buildings are separate from live listings.")
        selected_property_id = None
        selected_property = None
    else:
        search_cols = st.columns([1, 1.45], gap="medium")
        with search_cols[0]:
            center_slug = st.selectbox(
                "Search locality", locality_slugs, index=locality_slugs.index(preferred_center),
                format_func=lambda slug: str(localities.loc[localities["slug"] == slug, "name"].iloc[0]),
                key="live-map-center-v3",
                help="Choose the locality that anchors the search radius.",
            )
        locality_projects = listings[listings["locality_match_slug"].astype(str) == str(center_slug)].copy()
        property_ids = [int(value) for value in locality_projects["id"].dropna().tolist()]
        property_labels = {
            int(row.id): f"{row.project_name} · {row.builder_name or 'Builder not provided'}"
            for row in locality_projects.itertuples(index=False)
        }
        property_key = f"live-map-property-v1-{center_slug}"
        if locked_slug == center_slug and st.session_state.get("locked_project_id") in property_ids:
            st.session_state[property_key] = int(st.session_state["locked_project_id"])
        with search_cols[1]:
            selected_property_id = st.selectbox(
                "Search property · optional",
                options=[None, *property_ids],
                format_func=lambda project_id: "All properties in this locality" if project_id is None else property_labels.get(int(project_id), "Project"),
                key=property_key,
                help="Search projects in the selected locality. Selecting one filters the project pins; use Lock search to keep the radius focused on it.",
            )
        selected_property = locality_projects.loc[locality_projects["id"] == selected_property_id].iloc[0] if selected_property_id is not None else None
    center = localities.loc[localities["slug"] == center_slug].iloc[0]
    st.markdown("#### Lock a locality or a property radius")
    lock_col, status_col = st.columns([1, 2], vertical_alignment="center")
    if lock_col.button("Lock search & run analysis", key="lock-live-map-area-v2", type="primary"):
        st.session_state["locked_area_slug"] = center_slug
        st.session_state["locked_area_name"] = str(center["name"])
        st.session_state["locked_project_id"] = int(selected_property_id) if selected_property_id is not None else None
        st.session_state.pop("area_agent_review", None)
    if lock_col.button("Unlock area", key="unlock-live-map-area"):
        st.session_state.pop("locked_area_slug", None)
        st.session_state.pop("locked_area_name", None)
        st.session_state.pop("locked_project_id", None)
        st.session_state.pop("area_agent_review", None)
    locked_slug = st.session_state.get("locked_area_slug")
    locked_in_market = bool(locked_slug and locked_slug in set(locality_slugs))
    if locked_in_market:
        locked_name = localities.loc[localities["slug"] == locked_slug, "name"]
        locked_project_label = property_labels.get(int(st.session_state.get("locked_project_id")), "") if st.session_state.get("locked_project_id") else ""
        status_col.success(f"Locked: {locked_project_label + ' · ' if locked_project_label else ''}{locked_name.iloc[0]} · radius and analysis follow this search")
    else:
        status_col.caption("Search a locality, optionally narrow to a project, then lock the radius. The supervisor report appears here.")

    price_low = listings["price_min"].fillna(listings["price_max"])
    price_high = listings["price_max"].fillna(listings["price_min"])
    price_values = pd.concat([price_low, price_high]).dropna().astype(float)
    filter_cols = st.columns([1, 1, 1.4, 1])
    radius_km = filter_cols[0].slider("Search radius (km)", 1, 50, 20, key="live-map-radius-v2")
    if zoomedin_demo_mode:
        filter_cols[1].caption("Map scale: block / building footprint · `zoomedin_demo`")
        zoom = 17.2
    else:
        zoom_label = filter_cols[1].selectbox("Map scale", ["District overview", "Locality", "Street / precinct"], index=1, key="live-map-scale-v2")
        zoom = {"District overview": 9.5, "Locality": 12.5, "Street / precinct": 15.5}[zoom_label]
    if not price_values.empty:
        minimum_cr = max(0.0, round(float(price_values.min()) / 10_000_000, 1))
        maximum_cr = max(minimum_cr + 0.1, round(float(price_values.max()) / 10_000_000, 1))
        if maximum_cr <= minimum_cr:
            maximum_cr = minimum_cr + 0.1
        price_range = filter_cols[2].slider(
            "Listing price range (₹ Cr)", min_value=minimum_cr, max_value=maximum_cr,
            value=(minimum_cr, maximum_cr), step=0.1, key="live-map-price-range",
        )
        include_unpriced = filter_cols[3].toggle("Include unpriced", value=False, key="live-map-include-unpriced")
        in_price = (price_high >= price_range[0] * 10_000_000) & (price_low <= price_range[1] * 10_000_000)
        if include_unpriced:
            in_price |= price_low.isna() & price_high.isna()
        matching_listings = listings[in_price].copy()
    else:
        matching_listings = listings.copy()
        filter_cols[2].caption("No listing prices in this market")
        filter_cols[3].caption("All active listings shown")
    project_types = sorted(listings["project_type"].dropna().astype(str).unique().tolist())
    selected_types = st.multiselect("Project types", project_types, default=project_types, key="live-map-project-types")
    if selected_types:
        matching_listings = matching_listings[matching_listings["project_type"].isin(selected_types)]
    else:
        matching_listings = matching_listings.iloc[0:0]
    heatmap_listings = matching_listings.copy()
    if selected_property_id is not None:
        matching_listings = matching_listings[matching_listings["id"] == int(selected_property_id)]

    map_center_lat = float(center["latitude"])
    map_center_lon = float(center["longitude"])
    if selected_property is not None:
        selected_point_frame = property_map_points(pd.DataFrame([selected_property.to_dict()]), localities)
        if not selected_point_frame.empty:
            map_center_lat = float(selected_point_frame.iloc[0]["latitude"])
            map_center_lon = float(selected_point_frame.iloc[0]["longitude"])

    locked_row = None
    if locked_in_market:
        locked_row = localities.loc[localities["slug"] == locked_slug].iloc[0]
        try:
            locked_doc = load_area_dna(str(locked_row["slug"]), str(locked_row.get("mongodb_id") or "")) or {}
        except Exception:
            locked_doc = {}
        locked_area_listings = listings[listings["locality_match_slug"] == locked_slug]
        with st.container(border=True):
            show_area_agent_report(db, user, locked_row, locked_doc, locked_area_listings)
    else:
        st.info("Choose an area and select **Lock & run area analysis** to see source-backed market agents, an impact pathway, and a supervisor draft here.", icon=":material/lock_open:")

    hsr_demo_parcels = pd.DataFrame()
    if zoomedin_demo_mode:
        st.warning("SYNTHETIC ZOOM-IN DEMO · HSR Layout · Every parcel, building footprint, price, unit count and trend in this overlay is invented and labeled `zoomedin_demo`.")
        hsr_demo_parcels = make_hsr_zoomin_demo(float(center["latitude"]), float(center["longitude"]))
        demo_price_bounds = (
            int(hsr_demo_parcels["asking_psf"].min() // 500 * 500),
            int((hsr_demo_parcels["asking_psf"].max() + 499) // 500 * 500),
        )
        demo_asking_range = st.slider(
            "Synthetic building ask filter (₹/sq ft) · zoomedin_demo",
            min_value=demo_price_bounds[0], max_value=demo_price_bounds[1],
            value=demo_price_bounds, step=500, key="hsr-zoomedin-demo-price-filter",
        )
        hsr_demo_parcels = hsr_demo_parcels[
            hsr_demo_parcels["asking_psf"].between(*demo_asking_range)
        ].copy()

    heatmap_control_cols = st.columns([1, 1.7, 2.4])
    show_heatmap = heatmap_control_cols[0].toggle("Locality heatmap", value=True, key="live-map-show-heatmap")
    heatmap_signal = heatmap_control_cols[1].selectbox(
        "Heatmap signal", ["Project coverage", "Locality score"], key="live-map-heat-signal",
        help="Project coverage uses active catalogue rows; locality score uses the connected score field. Neither is a property valuation.",
    )
    heatmap_control_cols[2].caption("Heat colors show the selected signal. Pins retain project names and hover details.")
    localities["distance_km"] = localities.apply(
        lambda row: distance_km(map_center_lat, map_center_lon, float(row["latitude"]), float(row["longitude"])),
        axis=1,
    )
    nearby = localities[localities["distance_km"] <= radius_km].copy()
    listing_counts = matching_listings["locality_match_slug"].dropna().value_counts().to_dict()
    heatmap_counts = heatmap_listings["locality_match_slug"].dropna().value_counts().to_dict()
    localities["matching_listings"] = localities["slug"].map(listing_counts).fillna(0).astype(int)
    localities["heatmap_listings"] = localities["slug"].map(heatmap_counts).fillna(0).astype(int)
    if heatmap_signal == "Project coverage":
        maximum_count = max(heatmap_counts.values(), default=0)
        localities["heat_weight"] = localities["heatmap_listings"].map(lambda count: float(count) / maximum_count if maximum_count else 0.0)
    else:
        score_values = pd.to_numeric(localities["composite_score"], errors="coerce")
        score_scale = 100.0 if score_values.max(skipna=True) > 10 else 10.0
        localities["heat_weight"] = (score_values / score_scale).clip(0, 1).fillna(0.0)
    nearby = nearby.copy()
    nearby["matching_listings"] = nearby["slug"].map(listing_counts).fillna(0).astype(int)
    nearby["heat_weight"] = nearby["slug"].map(dict(zip(localities["slug"], localities["heat_weight"]))).fillna(0.0)
    # Show every mapped locality inside the search radius, including areas with no exact listing.
    map_points = nearby.copy()
    map_points["point_radius_m"] = map_points.apply(lambda row: 280 if row["slug"] == center_slug else min(650, 130 + 55 * int(row["matching_listings"])), axis=1)
    map_points["fill_color"] = map_points.apply(
        lambda row: [245, 158, 11, 235] if row["slug"] == center_slug else ([13, 148, 136, 220] if row["matching_listings"] > 0 else [100, 116, 139, 155]), axis=1)
    map_points["map_color"] = map_points.apply(lambda row: "#f59e0b" if row["slug"] == center_slug else ("#0d9488" if row["matching_listings"] > 0 else "#94a3b8"), axis=1)
    map_points["map_size"] = map_points["matching_listings"].map(lambda count: min(55, 18 + int(count) * 8)).astype(float)
    map_points["distance_display"] = map_points["distance_km"].map(lambda value: f"{value:.1f} km")
    map_points["detail"] = map_points.apply(
        lambda row: f"{row['distance_display']} from center · {row['matching_listings']} matched listings · locality score {float(row['composite_score']):.1f}/10" if pd.notna(row["composite_score"]) else f"{row['distance_display']} from center · {row['matching_listings']} matched listings · score not available",
        axis=1,
    )
    listing_price_ranges = {}
    for slug, group in matching_listings.dropna(subset=["locality_match_slug"]).groupby("locality_match_slug"):
        values = pd.concat([group["price_min"], group["price_max"]]).dropna()
        if not values.empty:
            listing_price_ranges[str(slug)] = f" · listed {money(values.min(), True)}–{money(values.max(), True)}"
    map_points["detail"] = map_points.apply(lambda row: row["detail"] + listing_price_ranges.get(str(row["slug"]), " · no listed price"), axis=1)
    map_points["name"] = map_points["name"].astype(str)
    project_map_source = matching_listings[matching_listings["locality_match_slug"].astype(str).isin(set(nearby["slug"].astype(str)))].copy()
    project_points = property_map_points(project_map_source, nearby)
    poi_points = pd.DataFrame()
    if locked_in_market and locked_row is not None:
        poi_points = intelligence_demo_pois(
            map_center_lat, map_center_lon, radius_km, str(locked_row["slug"])
        )
    clicked_slug = None
    clicked_demo_parcel = None
    if zoomedin_demo_mode:
        if not hsr_demo_parcels.empty:
            valid_ids = set(hsr_demo_parcels["parcel_id"])
            selected_demo_id = st.session_state.get("hsr-zoomedin-demo-selected-parcel")
            if selected_demo_id not in valid_ids:
                selected_demo_id = hsr_demo_parcels.iloc[0]["parcel_id"]
            hsr_fig = hsr_demo_figure(hsr_demo_parcels, float(center["latitude"]), float(center["longitude"]), str(selected_demo_id))
            st.plotly_chart(hsr_fig, width="stretch", key="hsr-zoomedin-demo-map-v2",
                            config={"displayModeBar": True, "scrollZoom": True},
                            alt="Zoomable HSR synthetic building-block map with hover details for each sample parcel")
            st.caption("Land use colors: residential (teal), mixed use (orange), commercial (blue). Each parcel is a generated polygon; the smaller gold inset is a sample building footprint. Hover for asking price, floors, units, absorption and trend. Drag to zoom and use the toolbar to reset.")
    else:
        st.markdown(
            f'<div class="pc-map-context"><small>Area lens · {radius_km} km radius</small><strong>{html.escape(str(locked_row["name"] if locked_in_market and locked_row is not None else center["name"]))}</strong><span>{"Area locked · amenity points and analysis are scoped to this locality." if locked_in_market else "Lock this locality to reveal a radius of schools, healthcare, work hubs, transit and lifestyle signals."}</span></div>',
            unsafe_allow_html=True,
        )
        render_circle_map(
            map_center_lat, map_center_lon, float(zoom), float(radius_km),
            localities=map_points, projects=project_points, pois=poi_points if not poi_points.empty else None,
            height=570,
            alt=f"Map of {center['city']} with visible approximate property circles, locality markers, and radius amenity points",
            show_heatmap=show_heatmap,
        )
        radius_anchor = str(selected_property["project_name"]) if selected_property is not None else str(center["name"])
        st.caption(f"The radius is centered on {radius_anchor}. A selected project uses its approximate locality-offset demo coordinate; otherwise the locality centroid is used. Exact project coordinates are not in the source catalogue. Hover project and amenity circles for details.")
        st.markdown("<div class='pc-map-badges' style='justify-content:flex-start;margin:.5rem 0 1rem'><span class='pc-map-badge' style='color:#0d9488'>● Project circles</span><span class='pc-map-badge' style='color:#e99b13'>● Selected locality</span><span class='pc-map-badge' style='color:#5b8def'>● Radius amenities</span><span class='pc-map-badge'>◌ Search boundary</span></div>", unsafe_allow_html=True)
        # Keep the assistant entry point beside the map so it reads as a map tool.
        map_assist_col = st.columns([1, 2, 1])[1]
        with map_assist_col.popover("✦  Ask PropClue about this area", type="primary", width="stretch", help="Discuss property details, nearby signals, legal evidence, bank status or investment scenarios for the selected map area"):
            st.markdown("#### Your area, in context")
            st.caption("Ask about nearby schools, hospitals, tech parks, petrol bunks, airport, railway, malls and public transport. You can also ask about legal/title evidence, bank approval or 1/3/5-year scenarios.")
            show_assistant(db, user, city, with_header=False, floating=True)
    radius_table = nearby.sort_values("distance_km").head(15).copy()
    if not radius_table.empty:
        radius_table["Project records"] = radius_table["slug"].map(listing_counts).fillna(0).astype(int)
        st.markdown(f"**Radius coverage · nearest {len(radius_table)} locality records**")
        st.dataframe(radius_table[["name", "distance_km", "Project records", "composite_score", "connectivity_score"]].rename(columns={"name":"Locality", "distance_km":"Distance km", "composite_score":"Area score / 10", "connectivity_score":"Connectivity / 10"}), width="stretch", hide_index=True, height=280, alt="Nearest locality scores and exact matched project counts inside the radius search")
    focus_slug = clicked_slug if clicked_slug in set(localities["slug"]) else (str(locked_slug) if locked_in_market else center_slug)
    focus = localities.loc[localities["slug"] == focus_slug].iloc[0]
    nearby_slugs = set(nearby["slug"])
    radius_listings = matching_listings[matching_listings["locality_match_slug"].isin(nearby_slugs)].copy()
    focus_listings = radius_listings[radius_listings["locality_match_slug"] == focus_slug].copy()
    unlocated_count = int(matching_listings["locality_match_slug"].isna().sum())

    stats = st.columns(4)
    stats[0].metric("Localities in radius", f"{len(nearby):,}", border=True)
    stats[1].metric("Listings in radius", f"{len(radius_listings):,}", border=True)
    stats[2].metric("Listings without exact area match", f"{unlocated_count:,}", border=True)
    stats[3].metric("Center locality score", f"{float(center['composite_score']):.1f}/10" if pd.notna(center["composite_score"]) else "Not scored", border=True)
    st.markdown("**Map legend**　🟠 selected area　🟢 locality with matching active projects　⚪ no exact project match")

    # Readable, source-aware locality intelligence is visible without relying on a hover event.
    try:
        focus_doc = load_area_dna(str(focus["slug"]), str(focus.get("mongodb_id") or "")) or {}
    except Exception:
        focus_doc = {}
    infra = focus_doc.get("infrastructure", {}) if isinstance(focus_doc, dict) else {}
    future = focus_doc.get("future_potential", {}) if isinstance(focus_doc, dict) else {}
    gaps = focus_doc.get("gap_analysis", {}) if isinstance(focus_doc, dict) else {}
    market = focus_doc.get("real_estate", {}) if isinstance(focus_doc, dict) else {}
    brief_name = str(selected_property["project_name"]) if selected_property is not None else str(focus["name"])
    st.markdown(f"### Area / Project Brief · {brief_name}")
    st.caption("Hover property circles for listing, bank-evidence and approximate age details. Hover amenity circles for the nearest radius signals. Grey locality circles still show coverage without an exact listing match.")
    quality_label = focus_doc.get("meta", {}).get("data_quality", "not specified") if isinstance(focus_doc, dict) else "not specified"
    intel_cols = st.columns(5)
    intel_cols[0].metric("Area score", f"{float(focus['composite_score']):.1f}/10" if pd.notna(focus["composite_score"]) else "Not scored", border=True)
    intel_cols[1].metric("Connectivity", f"{float(focus['connectivity_score']):.1f}/10" if pd.notna(focus["connectivity_score"]) else "Not scored", border=True)
    intel_cols[2].metric("Apartment ask · Mongo", f"₹{float(market['avg_sale_psf_apartment']):,.0f}/sq ft" if isinstance(market, dict) and market.get("avg_sale_psf_apartment") else "Not stored", border=True)
    trend = market.get("price_trend_pct_6m") if isinstance(market, dict) else None
    intel_cols[3].metric("6-month price trend", f"{float(trend):+.1f}%" if trend is not None else "Not stored", border=True)
    intel_cols[4].metric("Matched active projects", f"{int(focus.get('matching_listings', 0)):,}", border=True)
    focus_pois = intelligence_demo_pois(map_center_lat, map_center_lon, radius_km, str(focus["slug"]))
    age_identity = f"project:{int(selected_property['id'])}" if selected_property is not None else f"locality:{focus['slug']}"
    age_seed = int(hashlib.sha256(f"{age_identity}:representative-age:intelligence_demo".encode()).hexdigest()[:8], 16)
    representative_age = 1 + age_seed % 35
    st.session_state["assistant_map_context"] = {
        "name": str(selected_property["project_name"]) if selected_property is not None else str(focus["name"]),
        "project_name": str(selected_property["project_name"]) if selected_property is not None else "",
        "slug": str(focus["slug"]),
        "latitude": map_center_lat, "longitude": map_center_lon,
        "radius_km": float(radius_km),
    }
    render_area_project_brief(
        brief_name, focus.get("lifestyle_score"), focus.get("connectivity_score"),
        representative_age, focus_pois, scope_label="Area / Project Brief · radius and quality signals",
    )
    project_tailwinds = []
    if isinstance(future, dict):
        project_tailwinds += [str(x) for x in future.get("upcoming_metro_projects", [])]
        project_tailwinds += [str(x) for x in future.get("upcoming_infra_other", [])]
    gap_items = gaps.get("top_3_gaps") or gaps.get("missing_sectors") or [] if isinstance(gaps, dict) else []
    with st.container(border=True):
        st.markdown(f"#### Area brief <span style='color:#0D9488;font-size:.8rem'>SOURCE QUALITY: {quality_label}</span>", unsafe_allow_html=True)
        signal_cols = st.columns(3, gap="medium")
        with signal_cols[0].container(border=True):
            st.markdown("**:material/account_balance: Infrastructure**")
            if isinstance(infra, dict) and infra:
                for key in ["road_quality", "water_supply", "power_supply", "flood_risk", "sewage_quality"]:
                    if infra.get(key) is not None:
                        st.write(f"**{key.replace('_',' ').title()}:** {infra[key]}")
            else:
                st.caption("No infrastructure fields stored.")
        with signal_cols[1].container(border=True):
            st.markdown("**:material/trending_up: Future potential**")
            if project_tailwinds:
                for item in project_tailwinds[:4]:
                    st.markdown(f"- {item}")
            else:
                st.caption("No future infrastructure items stored.")
            outlook = future.get("appreciation_forecast_pct_3yr") if isinstance(future, dict) else None
            if outlook is not None:
                st.caption(f"MongoDB outlook field: {outlook}% over 3 years · not a verified forecast")
        with signal_cols[2].container(border=True):
            st.markdown("**:material/search: Watch items**")
            if gap_items:
                for item in list(gap_items)[:4]:
                    st.markdown(f"- {item}")
            else:
                st.caption("No locality gaps stored.")

    # Persona-specific copy: a buyer session never receives builder guidance.
    if user.role == "buyer":
        persona_title = "Buyer takeaway"
        persona_copy = gaps.get("b2c_recommendation") if isinstance(gaps, dict) else None
        if not persona_copy:
            persona_copy = (f"The database has {len(focus_listings)} active project record(s) exactly matched to this locality. Compare source dates, asking ranges and legal documents before deciding." if len(focus_listings) else f"No active project is exactly matched to {focus['name']} in the current filters. Use nearby localities below as separate comparisons; this is a catalogue coverage gap, not proof there are no homes for sale.")
    elif user.role == "builder":
        persona_title = "Builder opportunity"
        persona_copy = gaps.get("builder_recommendation") if isinstance(gaps, dict) else None
        if not persona_copy:
            persona_copy = (f"The filtered catalogue shows {len(focus_listings)} exact-match project record(s). Validate verified available inventory, absorption, land constraints and competing launches before changing price or supply." if len(focus_listings) else f"No active project is exactly matched to this locality in the current filters. Treat that as a data-coverage gap; validate competing supply and land records before inferring an opportunity.")
    else:
        persona_title = "Area readout"
        persona_copy = f"{len(focus_listings)} active project record(s) exactly match this locality under current filters."
    st.info(f"**{persona_title}**\n\n{persona_copy}", icon=":material/lightbulb:")
    st.caption(f"Area indicators are MongoDB database fields (quality: {quality_label}); locality score and project counts come from PostgreSQL. The source may include generated or unverified values.")

    st.caption("Live pins are published locality centroids, and radius distance is straight-line between centroids. Project listings have no latitude/longitude, and the source has no district or parcel boundaries. The HSR overlay, when enabled, is a bounded synthetic prototype and is never joined to real listings.")
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        st.markdown(f"#### {focus['name']} · {len(focus_listings)} matching listings")
        if focus_listings.empty:
            st.info(f"No active project is exactly matched to {focus['name']} under the current filters. The locality still has area intelligence above; the grey map pins indicate other areas without an exact project match.")
            populated_localities = localities[localities["matching_listings"] > 0].sort_values("distance_km")
            nearby_with_projects = populated_localities[populated_localities["distance_km"] <= radius_km].head(5)
            if nearby_with_projects.empty:
                nearby_with_projects = populated_localities[populated_localities["slug"] != focus_slug].head(5)
            if not nearby_with_projects.empty:
                st.markdown("**Closest localities with active projects**")
                st.caption("These are separate locality records; listings are not attributed to the selected area. Distances use locality centroids as a rough proxy; an item may be beyond the selected radius.")
                neighbor_slugs = nearby_with_projects["slug"].tolist()
                neighbor_projects = matching_listings[matching_listings["locality_match_slug"].isin(neighbor_slugs)].copy()
                distance_by_slug = dict(zip(nearby_with_projects["slug"], nearby_with_projects["distance_km"]))
                neighbor_projects["Locality"] = neighbor_projects["locality_match_slug"].map(dict(zip(localities["slug"], localities["name"])))
                neighbor_projects["Centroid distance km"] = neighbor_projects["locality_match_slug"].map(distance_by_slug).round(1)
                neighbor_projects["Price range"] = neighbor_projects.apply(lambda row: "–".join(money(v, True) for v in [row["price_min"], row["price_max"]] if pd.notna(v)) or "Not listed", axis=1)
                st.dataframe(neighbor_projects[["project_name", "Locality", "builder_name", "Price range", "Centroid distance km"]].rename(columns={"project_name":"Project", "builder_name":"Builder"}), width="stretch", hide_index=True, height=230, alt="Nearest locality records with live project listings; not assigned to the selected locality")
            else:
                unlocated_rows = matching_listings[matching_listings["locality_match_slug"].isna()].copy()
                if not unlocated_rows.empty:
                    st.markdown("**Live project records without a safe locality match**")
                    st.caption("These records exist in PostgreSQL, but their locality slug does not uniquely match a published locality. They are not pinned or counted inside this radius until the source mapping is corrected.")
                    unlocated_rows["Price range"] = unlocated_rows.apply(lambda row: "–".join(money(v, True) for v in [row["price_min"], row["price_max"]] if pd.notna(v)) or "Not listed", axis=1)
                    unlocated_rows["Source locality"] = unlocated_rows["locality_slug"].fillna("Not provided")
                    st.dataframe(unlocated_rows[["project_name", "Source locality", "builder_name", "Price range"]].head(8).rename(columns={"project_name":"Project", "builder_name":"Builder"}), width="stretch", hide_index=True, alt="Active source projects that are not assigned to a locality because no exact match is available")
                else:
                    st.caption("There are no project records matching these filters in this radius. Increase the radius, include unpriced projects, or review another locality.")
        else:
            selected_listing_id = st.selectbox(
                "Project / building record", focus_listings["id"].astype(int).tolist(),
                format_func=lambda listing_id: str(focus_listings.loc[focus_listings["id"] == listing_id, "project_name"].iloc[0]),
                key=f"live-map-listing-{focus_slug}",
            )
            selected_listing = focus_listings.loc[focus_listings["id"] == selected_listing_id].iloc[0]
            st.caption(f"**{selected_listing['project_name']}** · {selected_listing['builder_name'] or 'Builder not provided'} · {selected_listing['address'] or 'Address not provided'}")
            details = focus_listings[["project_name", "builder_name", "project_type", "price_min", "price_max", "rera_number", "address", "source", "updated_at"]].copy()
            details["Bank approval"] = "Not verified · no letter attached"
            details["Price range"] = details.apply(
                lambda row: "–".join(money(value, True) for value in [row["price_min"], row["price_max"]] if pd.notna(value)) or "Not listed",
                axis=1,
            )
            details = details[["project_name", "builder_name", "project_type", "Price range", "rera_number", "Bank approval", "address", "source", "updated_at"]].rename(columns={
                "project_name": "Project", "builder_name": "Builder", "project_type": "Type", "rera_number": "RERA reference",
                "address": "Stored address", "source": "Source", "updated_at": "Updated",
            })
            st.dataframe(details, width="stretch", hide_index=True, height=280, alt=f"Live PropClue listings associated with {focus['name']}")
            try:
                focus_doc_for_project = load_area_dna(str(selected_listing.get("locality_match_slug") or "")) or {}
            except Exception:
                focus_doc_for_project = {}
            proj_real_estate = focus_doc_for_project.get("real_estate", {}) if isinstance(focus_doc_for_project, dict) else {}
            project_age = 1 + int(hashlib.sha256(f"listing-map:{int(selected_listing['id'])}".encode()).hexdigest()[12:16], 16) % 35
            render_area_project_brief(
                str(selected_listing["project_name"]), focus.get("lifestyle_score"), focus.get("connectivity_score"),
                project_age, focus_pois, scope_label="Property Brief · final demo rating",
            )
            render_property_legal_and_returns(
                str(selected_listing["project_name"]), selected_listing.get("price_min"),
                selected_listing.get("price_max"), proj_real_estate.get("rental_yield") if isinstance(proj_real_estate, dict) else None,
            )
    with right:
        st.markdown("#### Compare localities")
        compare_options = nearby.sort_values("distance_km").drop_duplicates("slug")
        compare_slugs = compare_options["slug"].astype(str).tolist()
        default_compare = [center_slug] if center_slug in compare_slugs else (compare_slugs[:1])
        selected_compare = st.multiselect(
            "Choose up to four areas", compare_slugs, default=default_compare, max_selections=4,
            format_func=lambda slug: str(localities.loc[localities["slug"] == slug, "name"].iloc[0]),
            key=f"live-map-compare-{market_slug}-{center_slug}",
        )
        comparison_rows = []
        for slug in selected_compare:
            area = localities.loc[localities["slug"] == slug].iloc[0]
            area_listings = matching_listings[matching_listings["locality_match_slug"] == slug]
            try:
                area_doc = load_area_dna(str(area["slug"]), str(area["mongodb_id"] or ""))
            except Exception:
                area_doc = None
            real_estate = (area_doc or {}).get("real_estate", {})
            area_prices = pd.concat([area_listings["price_min"], area_listings["price_max"]]).dropna().astype(float)
            comparison_rows.append({
                "Locality": area["name"], "Distance km": round(float(area["distance_km"]), 1),
                "Listings in filter": len(area_listings),
                "Composite / 10": round(float(area["composite_score"]), 1) if pd.notna(area["composite_score"]) else None,
                "Apartment ₹/sq ft · Mongo": real_estate.get("avg_sale_psf_apartment"),
                "6-month price trend · Mongo": real_estate.get("price_trend_pct_6m"),
                "Rental yield · Mongo": real_estate.get("rental_yield"),
                "Listed price range": f"{money(area_prices.min(), True)}–{money(area_prices.max(), True)}" if not area_prices.empty else "Not available",
            })
        if comparison_rows:
            comparison = pd.DataFrame(comparison_rows)
            st.dataframe(comparison, width="stretch", hide_index=True, alt="Side-by-side live locality metrics and matched listing price ranges")
            st.caption("Area score comes from PostgreSQL. Price-per-square-foot, trend and yield fields come from the MongoDB area document and retain its source quality. Listing price range is taken from active PostgreSQL listings.")
        else:
            st.caption("Choose areas within the current radius to compare their database-backed indicators.")

    if zoomedin_demo_mode:
        st.divider()
        st.subheader("HSR Layout parcel/building prototype · zoomedin_demo")
        st.caption("This isolated synthetic layer demonstrates the analytics possible after verified building/parcel GIS is onboarded. All labels carry the `zoomedin_demo` suffix; none are real project or land records.")
        if hsr_demo_parcels.empty:
            st.info("No mock parcels fall in the selected synthetic price band.")
        else:
            if clicked_demo_parcel in set(hsr_demo_parcels["parcel_id"]):
                st.session_state["hsr-zoomedin-demo-selected-parcel"] = clicked_demo_parcel
            parcel_ids = hsr_demo_parcels["parcel_id"].tolist()
            default_parcel = st.session_state.get("hsr-zoomedin-demo-selected-parcel")
            if default_parcel not in parcel_ids:
                default_parcel = parcel_ids[0]
            selected_parcel_id = st.selectbox(
                "Inspect mock building / parcel",
                parcel_ids, index=parcel_ids.index(default_parcel),
                format_func=lambda parcel_id: str(hsr_demo_parcels.loc[hsr_demo_parcels["parcel_id"] == parcel_id, "name"].iloc[0]),
                key="hsr-zoomedin-demo-selected-parcel",
            )
            selected_parcel = hsr_demo_parcels.loc[hsr_demo_parcels["parcel_id"] == selected_parcel_id].iloc[0]
            demo_metrics = st.columns(4)
            demo_metrics[0].metric("Mock parcels", f"{len(hsr_demo_parcels):,}", border=True)
            demo_metrics[1].metric("Median synthetic ask", f"₹{hsr_demo_parcels['asking_psf'].median():,.0f}/sq ft", border=True)
            demo_metrics[2].metric("Sample available units", f"{int(hsr_demo_parcels['available_units'].sum()):,}", border=True)
            demo_metrics[3].metric("Median mock absorption", f"{hsr_demo_parcels['absorption_pct'].median():.1f}%/month", border=True)
            detail_cols = st.columns(4)
            detail_cols[0].metric("Land use · mock", selected_parcel["land_use"], border=True)
            detail_cols[1].metric("Floors · mock", str(int(selected_parcel["floors"])), border=True)
            detail_cols[2].metric("Mock homes", str(int(selected_parcel["units"])), border=True)
            detail_cols[3].metric("Mock YoY change", f"{selected_parcel['yoy_pct']:+.1f}%", border=True)
            above_baseline = int((hsr_demo_parcels["asking_psf"] > 12_000).sum())
            strongest = hsr_demo_parcels.sort_values("absorption_pct", ascending=False).iloc[0]
            st.info(
                f"Synthetic insight: {above_baseline} of {len(hsr_demo_parcels)} mock parcels are above the ₹12,000/sq ft demo reference; "
                f"{strongest['name']} has the highest modeled absorption at {strongest['absorption_pct']:.1f}%/month. These are invented scenario values."
            )
            land_use_summary = hsr_demo_parcels.groupby("land_use", as_index=False).agg(
                median_asking_psf=("asking_psf", "median"), sample_parcels=("parcel_id", "count"),
            )
            land_use_summary = land_use_summary.rename(columns={"land_use": "Land use · mock", "median_asking_psf": "Median mock ask · ₹/sq ft", "sample_parcels": "Mock parcels"})
            st.dataframe(land_use_summary, width="stretch", hide_index=True,
                         alt="Synthetic HSR Layout parcel counts and median asking price by mock land-use category")
            st.caption(f"Selected mock IDs: {selected_parcel['parcel_id']} / {selected_parcel['building_id']}. Suffix `zoomedin_demo` marks all generated geometry and insight values.")

    if st.button("Record this map search", key="live-map-record-search"):
        add_event(db, user.id, "Map area searched", locality=str(center["name"]),
                  context={"market_slug": market_slug, "radius_km": radius_km,
                           "price_range_cr": price_range if not price_values.empty else None,
                           "matched_listings": len(matching_listings), "source": "live PropClue database"})
        st.success("Map search saved to this demo profile's interaction history.")
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
    nearby_terms = ("nearby", "nearest", "radius", "school", "hospital", "tech park", "petrol", "airport", "railway", "mall", "transport", "metro")
    if any(term in q for term in nearby_terms):
        ctx = st.session_state.get("assistant_map_context")
        if isinstance(ctx, dict):
            nearby_pois = intelligence_demo_pois(float(ctx["latitude"]), float(ctx["longitude"]), float(ctx.get("radius_km", 10)), str(ctx["slug"]))
            if any(token in q for token in ("school", "hospital", "tech", "petrol", "airport", "railway", "mall", "transport", "metro")):
                matches = nearby_pois[nearby_pois["category"].map(lambda category: str(category).casefold() in q or (category == "Public transport" and any(k in q for k in ("metro", "bus", "transit", "transport"))))]
            else:
                matches = nearby_pois
            if matches.empty:
                matches = nearby_pois
            lines = [f"- **{row.category}:** {row.name} · {row.distance_km:.1f} km straight-line" for row in matches.itertuples(index=False)]
            return (f"Radius scan around **{ctx['name']}** · {float(ctx.get('radius_km', 10)):.0f} km.\n\n"
                    + "\n".join(lines)
                    + "\n\nThese `intelligence_demo` points are synthetic stand-ins, not verified venues or travel routes. Check the map hover cards for each point.")
        return "Open Map Explorer and lock an area first. I can then summarize the selected radius. Nearby points are currently synthetic intelligence_demo placeholders, not verified venues or routes."
    if any(term in q for term in ("investment", "worthwhile", "worth it", "return", "roi", "1 year", "3 year", "5 year", "1y", "3y", "5y")):
        investment_project = matched_project
        if not investment_project:
            selected_id = st.session_state.get("selected_project")
            investment_project = db.get(Project, selected_id) if selected_id else None
        if investment_project:
            rent_yield = investment_project.monthly_rent * 12 / max(investment_project.asking_price, 1) * 100
            rows = []
            for years in (1, 3, 5):
                value = investment_project.asking_price * 1.05 ** years
                gross_rent = investment_project.asking_price * rent_yield / 100 * years
                total_return = ((value + gross_rent) / max(investment_project.asking_price, 1) - 1) * 100
                rows.append(f"- **{years} year{'s' if years > 1 else ''}:** illustrative value {money(value, True)} · gross rent {money(gross_rent, True)} · combined return {total_return:+.1f}%")
            return (f"### {investment_project.name} · illustrative investment scenario\n"
                    f"Assumptions: 5% annual value growth and {rent_yield:.1f}% gross rent yield from a synthetic demo record.\n\n"
                    + "\n".join(rows)
                    + "\n\nThis is not a forecast or investment recommendation. It excludes vacancy, taxes, transaction fees, financing, maintenance and resale costs; live rent and transaction histories are not connected.")
        return "Select a property or name a project and I can show an illustrative 1, 3 and 5 year scenario. It is not a forecast or investment recommendation."
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
                f"- **Illustrative distances:** School {proximity['school']} km · Hospital {proximity['hospital']} km · Tech park {proximity['it_park']} km · Petrol bunk {proximity['petrol_bunk']} km · Airport {proximity['airport']} km · Railway station {proximity['railway_station']} km · Mall {proximity['mall']} km · Public transport {proximity['public_transport']} km\n"
                f"- **Legal / bank evidence:** EC, Khata, title, RERA, tax and technical clearances are not connected; bank approval is not verified and no bank letter is attached.\n\n"
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
    st.caption("Ask about a project’s legal/title checklist, whether bank approval is verified, nearby locations in the selected radius, or an illustrative 1/3/5-year investment scenario. The demo has no official legal or bank feed and calls no external LLM.")
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
        st.caption("Legal/title status and bank approval are reported only from attached evidence; none is connected in this demo. Nearby POIs and investment horizons are clearly labeled scenarios.")
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
    top_header("Market overview · Live data", "Browse the connected PropClue project catalogue and linked locality intelligence in one place.")
    show_source_catalog(db, st.session_state.get("market_slug"), city)


def render_market_pulse(city_label: str, city_listings: pd.DataFrame, city_localities: pd.DataFrame) -> None:
    """Render a live-source summary with a continuously moving local-time dial."""
    status = city_listings["construction_status"].fillna("").astype(str).str.casefold().str.replace(r"[^a-z]", "", regex=True)
    units = pd.to_numeric(city_listings["units"], errors="coerce")
    rera_count = int(city_listings["rera_number"].fillna("").astype(str).str.strip().ne("").sum())
    priced = int((city_listings["price_min"].notna() | city_listings["price_max"].notna()).sum())
    under_construction = int(status.eq("underconstruction").sum())
    latest = pd.to_datetime(city_listings["updated_at"], errors="coerce").max()
    latest_text = latest.strftime("%d %b %Y") if pd.notna(latest) else "No update date stored"
    component_data = {
        "kpis": [
            {"label": "Live project records", "value": f"{len(city_listings):,}", "detail": f"{city_label} catalogue"},
            {"label": "Published localities", "value": f"{len(city_localities):,}", "detail": "Connected locality coverage"},
            {"label": "Units stated", "value": f"{int(units.sum()):,}" if units.notna().any() else "Not provided", "detail": f"Across {int(units.notna().sum()):,} listing records"},
            {"label": "RERA references", "value": f"{rera_count:,} / {len(city_listings):,}", "detail": "ID presence only; not verified"},
        ],
        "chips": [
            {"label": "Price on file", "value": f"{priced:,}"},
            {"label": "Under construction", "value": f"{under_construction:,}"},
            {"label": "Live demand feed", "value": "Not connected", "muted": True},
            {"label": "Verified sales", "value": "Not connected", "muted": True},
            {"label": "Source updated", "value": latest_text},
        ],
    }
    _MARKET_PULSE_COMPONENT(key=f"market-pulse-{re.sub(r'[^a-z0-9]+', '-', city_label.casefold())}", data=component_data)


def show_market(db, city: str, show_header: bool = True, user_role: str = "buyer"):
    """Live locality intelligence view with bounded, explainable comparisons."""
    if show_header:
        top_header("Market intelligence", "Compare verified database coverage and locality signals. Generated scenario analysis is clearly labeled.")
    try:
        localities, listings = load_catalog()
    except Exception as exc:
        st.error("Could not load live PropClue market data.")
        st.caption(f"Connection error type: {type(exc).__name__}.")
        return
    slug = st.session_state.get("market_slug", "")
    areas = localities[localities["city_slug"] == slug].copy()
    projects = listings[listings["city_slug"] == slug].copy()
    if areas.empty:
        st.info("No published locality records are available for this market.")
        return
    areas = areas.sort_values("name").reset_index(drop=True)
    names = dict(zip(areas["slug"], areas["name"]))
    active = projects["locality_match_slug"].dropna().value_counts().to_dict()
    areas["Active listings"] = areas["slug"].map(active).fillna(0).astype(int)
    options = areas["slug"].astype(str).tolist()
    selected = st.selectbox("Focus locality", options,
        format_func=lambda key: names.get(key, key), key="market-intelligence-focus")
    area = areas.loc[areas["slug"] == selected].iloc[0]
    try:
        doc = load_area_dna(str(area["slug"]), str(area.get("mongodb_id") or "")) or {}
    except Exception:
        doc = {}
    real = doc.get("real_estate", {}) if isinstance(doc, dict) else {}
    connectivity = doc.get("connectivity", {}) if isinstance(doc, dict) else {}
    cols = st.columns(4)
    cols[0].metric("Locality score", f"{float(area['composite_score']):.1f}/10" if pd.notna(area["composite_score"]) else "Not scored", border=True)
    cols[1].metric("Matched active listings", f"{int(area['Active listings']):,}", border=True)
    cols[2].metric("Apartment asking · Mongo", f"₹{float(real['avg_sale_psf_apartment']):,.0f}/sq ft" if isinstance(real, dict) and real.get("avg_sale_psf_apartment") else "Not available", border=True)
    trend = real.get("price_trend_pct_6m") if isinstance(real, dict) else None
    cols[3].metric("Stored 6-month trend", f"{float(trend):+.1f}%" if trend is not None else "Not available", border=True)
    price_rows = projects[projects["locality_match_slug"] == selected].copy()
    p = pd.concat([price_rows["price_min"], price_rows["price_max"]]).dropna()
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(f"#### {area['name']} · live catalogue")
        st.write(f"{len(price_rows)} active project records are exactly matched to this locality. Listed prices range from **{money(p.min(), True)} to {money(p.max(), True)}** where present." if not p.empty else f"{len(price_rows)} active project records are matched; listed price values are missing.")
        st.caption("Listing price range is sourced from PostgreSQL. It is a listing range, not a verified transaction benchmark.")
        if not price_rows.empty:
            st.dataframe(price_rows[["project_name", "builder_name", "project_type", "price_min", "price_max", "construction_status", "updated_at"]].rename(columns={"project_name":"Project", "builder_name":"Builder", "project_type":"Type", "price_min":"Price from", "price_max":"Price to", "construction_status":"Status", "updated_at":"Updated"}), width="stretch", hide_index=True, alt="Live project listings matched to the selected locality")
    with b:
        st.markdown("#### Locality signals")
        quality = doc.get("meta", {}).get("data_quality", "not specified") if isinstance(doc, dict) else "not specified"
        st.caption(f"MongoDB area document · source quality: **{quality}** · stored indicators are not independently verified.")
        signal_cols = st.columns(3)
        signal_cols[0].metric("Connectivity", f"{float(area['connectivity_score']):.1f}/10" if pd.notna(area["connectivity_score"]) else "Not stored", border=True)
        signal_cols[1].metric("Lifestyle", f"{float(area['lifestyle_score']):.1f}/10" if pd.notna(area["lifestyle_score"]) else "Not stored", border=True)
        signal_cols[2].metric("Real estate", f"{float(area['realestate_score']):.1f}/10" if pd.notna(area["realestate_score"]) else "Not stored", border=True)
        market_cards = st.columns(2)
        metro = connectivity.get("nearest_metro_station") if isinstance(connectivity, dict) else None
        metro_km = connectivity.get("nearest_metro_distance_km") if isinstance(connectivity, dict) else None
        market_cards[0].markdown("**Transit access**")
        market_cards[0].write(f"{metro or 'Metro station not listed'}" + (f" · {metro_km} km stored distance" if metro_km is not None else ""))
        rental = real.get("rental_yield") if isinstance(real, dict) else None
        trend_value = real.get("price_trend_pct_6m") if isinstance(real, dict) else None
        market_cards[1].markdown("**Market movement**")
        market_cards[1].write((f"6-month price trend {float(trend_value):+.1f}%" if trend_value is not None else "Trend not stored") + (f" · rental yield {rental}%" if rental is not None else ""))
        gaps = doc.get("gap_analysis", {}) if isinstance(doc, dict) else {}
        if user_role == "buyer":
            insight = gaps.get("b2c_recommendation") if isinstance(gaps, dict) else None
            if not insight:
                insight = f"Compare the stored area price and connectivity fields with current project listings in {area['name']}; listing coverage may be incomplete."
            st.info(f"**Buyer insight · {area['name']}**\n\n{insight}", icon=":material/lightbulb:")
        elif user_role == "builder":
            insight = gaps.get("builder_recommendation") if isinstance(gaps, dict) else None
            if not insight:
                insight = "Compare verified competing inventory, build costs and demand before changing pricing or supply."
            st.info(f"**Builder insight · {area['name']}**\n\n{insight}", icon=":material/insights:")
        st.caption("Causal impact modeling remains a labeled prototype. Verified rate, material-cost, and infrastructure time series are not connected.")


def show_compare_workspace(db, user: User):
    top_header("Compare & save", "Keep a shortlist, compare projects side by side, and follow your enquiries from one place.")
    show_saved(db, user, show_header=False)
    with st.expander("My enquiries and visits", expanded=False):
        show_buyer_requests(db, user)


def show_locality_intelligence(localities: pd.DataFrame, listings: pd.DataFrame, market_slug: str,
                              preferred_slug: str | None = None, selected_project: str | None = None) -> None:
    """Visible market-overview map and area signals, placed before the long listings table."""
    st.markdown("#### Locality intelligence · map and area signals")
    st.caption("Select an area to see its database scores, infrastructure, market indicators, and nearby project records. A zero project match means no exact catalogue join; it does not mean the locality has no property market.")
    areas = localities[localities["name"].notna()].copy().sort_values("name").reset_index(drop=True)
    if areas.empty:
        st.info("No published locality records are available in this market.")
        return
    counts = listings["locality_match_slug"].dropna().value_counts().to_dict()
    areas["Project records"] = areas["slug"].map(counts).fillna(0).astype(int)
    slugs = areas["slug"].astype(str).tolist()
    preferred = preferred_slug if preferred_slug in slugs else areas.sort_values(["Project records", "composite_score"], ascending=[False, False]).iloc[0]["slug"]
    if selected_project and preferred_slug not in slugs:
        st.warning(f"{selected_project} has no unique published-locality match for its source slug. Choose a locality below to view area context; it will not be labeled as the project's locality.")
    selected = st.selectbox("Focus locality", slugs, index=slugs.index(preferred),
        format_func=lambda slug: str(areas.loc[areas["slug"] == slug, "name"].iloc[0]),
        key=f"overview-locality-{market_slug}")
    area = areas.loc[areas["slug"] == selected].iloc[0]
    try:
        doc = load_area_dna(str(area["slug"]), str(area.get("mongodb_id") or "")) or {}
    except Exception as exc:
        doc = {}
        st.caption(f"MongoDB area details are unavailable ({type(exc).__name__}); PostgreSQL scores and map remain available.")
    real = doc.get("real_estate", {}) if isinstance(doc, dict) else {}
    infrastructure = doc.get("infrastructure", {}) if isinstance(doc, dict) else {}
    future = doc.get("future_potential", {}) if isinstance(doc, dict) else {}
    gaps = doc.get("gap_analysis", {}) if isinstance(doc, dict) else {}
    left, right = st.columns([1.4, 1], gap="large")
    with left:
        selected_metrics = st.columns(4)
        selected_metrics[0].metric("Composite score", f"{float(area['composite_score']):.1f}/10" if pd.notna(area["composite_score"]) else "Not scored", border=True)
        selected_metrics[1].metric("Connectivity", f"{float(area['connectivity_score']):.1f}/10" if pd.notna(area["connectivity_score"]) else "Not scored", border=True)
        selected_metrics[2].metric("Exact matched projects", int(area["Project records"]), border=True)
        psf = real.get("avg_sale_psf_apartment") if isinstance(real, dict) else None
        selected_metrics[3].metric("Apartment ask · Mongo", f"₹{float(psf):,.0f}/sq ft" if psf else "Not stored", border=True)
        st.markdown(f"**{area['name']}** · {area['city']} · {area['area_type'] or 'area type not recorded'}")
        st.caption(f"PostgreSQL locality updated: {area['data_last_updated'] or 'date not recorded'} · MongoDB quality label: {doc.get('meta', {}).get('data_quality', 'not specified') if isinstance(doc, dict) else 'not specified'}")
        coords = areas.dropna(subset=["latitude", "longitude"]).copy()
        if not coords.empty and pd.notna(area["latitude"]) and pd.notna(area["longitude"]):
            coords["distance_km"] = coords.apply(lambda row: distance_km(float(area["latitude"]), float(area["longitude"]), float(row["latitude"]), float(row["longitude"])), axis=1)
            radius = st.slider("Nearby locality radius", 2, 30, 10, key=f"overview-locality-radius-{market_slug}")
            shown = coords[coords["distance_km"] <= radius].copy()
            shown["Project records"] = shown["slug"].map(counts).fillna(0).astype(int)
            shown["map_size"] = shown["Project records"].map(lambda count: min(55, 18 + int(count) * 8)).astype(float)
            shown["map_color"] = shown.apply(lambda row: "#f59e0b" if row["slug"] == selected else ("#0d9488" if row["Project records"] else "#94a3b8"), axis=1)
            shown["hover_text"] = shown.apply(lambda row: f"{int(row['Project records'])} exactly matched active projects · composite score {float(row['composite_score']):.1f}/10" if pd.notna(row["composite_score"]) else f"{int(row['Project records'])} exactly matched active projects · score not available", axis=1)
            shown["fill_color"] = shown.apply(lambda row: [245, 158, 11, 235] if row["slug"] == selected else ([13, 148, 136, 220] if row["Project records"] else [100, 116, 139, 155]), axis=1)
            shown["name"] = shown["name"].astype(str)
            shown["detail"] = shown.apply(lambda row: f"{row['name']} · {row['distance_km']:.1f} km · {int(row['Project records'])} project records · score {float(row['composite_score']):.1f}/10" if pd.notna(row["composite_score"]) else f"{row['name']} · {row['distance_km']:.1f} km · {int(row['Project records'])} project records · score unavailable", axis=1)
            matched_slugs = set(shown["slug"].astype(str))
            project_points = property_map_points(listings[listings["locality_match_slug"].astype(str).isin(matched_slugs)], shown)
            render_circle_map(
                float(area["latitude"]), float(area["longitude"]), 11, float(radius),
                localities=shown, projects=project_points, height=490,
                alt=f"Locality and approximate property circles around {area['name']} with hover details",
            )
            st.caption("Property circles use stable approximate offsets around matched locality centroids because the source has no project coordinates. Hover a property circle for the listing details. Selected locality is orange; radius uses straight-line distance, not travel time.")
            nearby_view = shown.sort_values("distance_km").head(12)
            st.dataframe(nearby_view[["name", "distance_km", "Project records", "composite_score", "connectivity_score"]].rename(columns={"name":"Locality", "distance_km":"Distance km", "Project records":"Matched projects", "composite_score":"Area score / 10", "connectivity_score":"Connectivity / 10"}), width="stretch", hide_index=True, height=260, alt="Nearest localities and score details represented on the radius map")
        else:
            st.warning("This market has no stored locality coordinates, so the map cannot be drawn.")
    with right:
        st.markdown(f"#### Area brief · {area['name']}")
        quality = doc.get("meta", {}).get("data_quality", "not specified") if isinstance(doc, dict) else "not specified"
        st.caption(f"MongoDB area document · quality: {quality}")
        with st.container(border=True):
            st.markdown("**Infrastructure & services**")
            infra_keys = ["road_quality", "water_supply", "power_supply", "flood_risk", "sewage_quality"]
            infra_cols = st.columns(2)
            shown_infra = 0
            if isinstance(infrastructure, dict):
                for index, key in enumerate(infra_keys):
                    value = infrastructure.get(key)
                    if value is not None:
                        infra_cols[shown_infra % 2].markdown(f"**{key.replace('_',' ').title()}**\n\n{str(value).replace('_',' ').title()}")
                        shown_infra += 1
            if not shown_infra:
                st.caption("No infrastructure attributes are stored for this locality.")
        with st.container(border=True):
            st.markdown("**Future potential**")
            tailwinds = []
            if isinstance(future, dict):
                tailwinds += [str(x) for x in future.get("upcoming_metro_projects", [])]
                tailwinds += [str(x) for x in future.get("upcoming_infra_other", [])]
            if tailwinds:
                for item in tailwinds[:4]:
                    st.markdown(f"- {item}")
            else:
                st.caption("No pipeline entries stored.")
            forecast = future.get("appreciation_forecast_pct_3yr") if isinstance(future, dict) else None
            if forecast is not None:
                st.metric("Stored 3-year outlook field", f"{forecast}%", help="MongoDB field only; this is not a verified forecast.", border=True)
        gap_items = (gaps.get("top_3_gaps") or gaps.get("missing_sectors") or []) if isinstance(gaps, dict) else []
        with st.container(border=True):
            st.markdown("**Watch items**")
            if gap_items:
                for item in list(gap_items)[:4]:
                    st.markdown(f"- {item}")
            else:
                st.caption("No locality gaps are recorded.")
        if isinstance(gaps, dict):
            buyer_note = gaps.get("b2c_recommendation")
        else:
            buyer_note = None
        if not buyer_note:
            buyer_note = f"There are {int(area['Project records'])} exact-match active project record(s) in this catalogue. If this is zero, compare nearby locality records separately; it does not mean there are no homes for sale."
        st.info(f"**Buyer takeaway**\n\n{buyer_note}", icon=":material/lightbulb:")
        if not tailwinds and forecast is None and not gap_items:
            st.info("MongoDB has no future-potential or gap fields for this locality.")
        if doc:
            sources = doc.get("meta", {}).get("data_sources", [])
            st.caption("MongoDB sources: " + (", ".join(map(str, sources)) if sources else "not specified") + ". Stored outlooks and scores are not independently verified forecasts.")
    if "latitude" in area and pd.notna(area["latitude"]) and pd.notna(area["longitude"]):
        radius_km = float(radius) if "radius" in locals() else 10.0
        nearby = areas.copy()
        nearby["distance_km"] = nearby.apply(lambda row: distance_km(float(area["latitude"]), float(area["longitude"]), float(row["latitude"]), float(row["longitude"])) if pd.notna(row["latitude"]) and pd.notna(row["longitude"]) else None, axis=1)
        nearby["Project records"] = nearby["slug"].map(counts).fillna(0).astype(int)
        nearby = nearby[(nearby["distance_km"] <= radius_km) & (nearby["Project records"] > 0) & (nearby["slug"] != selected)].sort_values("distance_km").head(8)
        if not nearby.empty:
            st.markdown("**Nearby locality markets with active listings**")
            st.caption("These projects belong to their matched locality records. They are shown for context and are not counted as inventory in the selected area.")
            st.dataframe(nearby[["name", "distance_km", "Project records", "composite_score"]].rename(columns={"name":"Locality", "distance_km":"Centroid distance km", "composite_score":"Score / 10"}), width="stretch", hide_index=True, alt="Nearby distinct localities with exact matched active project counts")


def _builder_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _google_places_builder_signal(builder_name: str, city_name: str, api_key: str) -> dict | None:
    """Fetch a one-time Google Places candidate/rating; results are not cached or persisted."""
    response = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.googleMapsUri,places.attributions",
            "Content-Type": "application/json",
        },
        json={"textQuery": f"{builder_name} real estate developer {city_name}, India", "maxResultCount": 5, "languageCode": "en", "regionCode": "IN"},
        timeout=12,
    )
    if response.status_code != 200:
        raise ValueError(f"Google Places returned HTTP {response.status_code}.")
    results = response.json().get("places", [])
    if not results:
        return None
    exact = [place for place in results if _builder_key(place.get("displayName", {}).get("text", "")) == _builder_key(builder_name)]
    return (exact or results)[0]


def show_builder_score_panel(city_listings: pd.DataFrame, listing: pd.Series, city_slug: str):
    builder = str(listing.get("builder_name") or "").strip()
    st.markdown("#### Builder score · available evidence")
    if not builder:
        st.info("This project record has no builder name to evaluate.")
        return

    builder_rows = city_listings[city_listings["builder_name"].fillna("").map(_builder_key) == _builder_key(builder)].copy()
    statuses = builder_rows["construction_status"].fillna("").astype(str).str.casefold().str.replace(r"[^a-z]", "", regex=True)
    completed = int(statuses.isin(["completed", "readytomove", "readyforpossession"]).sum())
    pipeline = int(statuses.isin(["underconstruction", "newlaunch", "upcoming", "prelaunch"]).sum())
    rera = builder_rows["rera_number"].fillna("").astype(str).str.strip().ne("")
    rera_coverage = float(rera.mean() * 100) if len(builder_rows) else None

    try:
        directory = load_builder_directory(city_slug)
    except Exception:
        directory = pd.DataFrame()
    directory_match = directory[directory.get("builder_name", pd.Series(dtype=str)).fillna("").map(_builder_key) == _builder_key(builder)] if not directory.empty else pd.DataFrame()
    verified_rank = None
    if not directory_match.empty:
        candidate = directory_match.iloc[0]
        confidence = str(candidate.get("data_confidence") or "").casefold()
        if confidence == "web_verified" and pd.notna(candidate.get("rank")):
            verified_rank = int(candidate["rank"])

    api_key = private_setting("GOOGLE_PLACES_API_KEY")
    google_place = None
    google_error = None
    if api_key and st.button("Fetch live Google rating", key=f"google-builder-rating-{listing.get('id', builder)}"):
        try:
            market_name = str(listing.get("city_slug") or city_slug)
            market_name = "Bengaluru" if market_name == "bangalore" else market_name.replace("-", " ").title()
            google_place = _google_places_builder_signal(builder, market_name, api_key)
        except Exception as exc:
            google_error = type(exc).__name__

    all_completed = city_listings["construction_status"].fillna("").astype(str).str.casefold().str.replace(r"[^a-z]", "", regex=True).isin(["completed", "readytomove", "readyforpossession"])
    completed_counts = city_listings.assign(_builder=city_listings["builder_name"].fillna("").map(_builder_key), _complete=all_completed).groupby("_builder")["_complete"].sum()
    city_max = int(completed_counts.max()) if not completed_counts.empty else 0
    components: list[tuple[str, float, int]] = []
    if len(builder_rows):
        components.append(("Observed completed catalogue records", (completed / city_max * 100) if city_max else 0.0, 30))
        components.append(("RERA reference coverage", rera_coverage or 0.0, 20))
    if verified_rank is not None and not directory.empty:
        ranks = pd.to_numeric(directory["rank"], errors="coerce").dropna()
        max_rank = max(1, int(ranks.max())) if not ranks.empty else 1
        components.append(("City directory rank signal", max(0.0, (max_rank - verified_rank + 1) / max_rank * 100), 15))
    google_exact = False
    if google_place:
        place_name_for_match = google_place.get("displayName", {}).get("text", "")
        google_exact = _builder_key(place_name_for_match) == _builder_key(builder)
        if google_exact and google_place.get("rating") is not None:
            components.append(("Google Maps average rating", float(google_place["rating"]) / 5 * 100, 35))

    score = round(sum(value * weight for _, value, weight in components) / sum(weight for _, _, weight in components)) if components else None
    coverage = sum(weight for _, _, weight in components)
    metric_cols = st.columns(4)
    metric_cols[0].metric("Evidence score", f"{score}/100" if score is not None else "Not enough data", border=True, help="Weighted available-evidence index. It is not a verified builder-quality or investment rating.")
    metric_cols[1].metric("Completed records", str(completed), border=True, help="Completed/ready-to-move records currently present in this live active catalogue, not the builder's lifetime total.")
    metric_cols[2].metric("Current / upcoming", str(pipeline), border=True, help="Under-construction, new-launch, upcoming, or pre-launch records in the connected catalogue.")
    metric_cols[3].metric("Evidence coverage", f"{coverage}%", border=True, help="Share of possible component weight with a source value available. Google ratings add coverage only when an exact name match is returned.")
    if components:
        detail = pd.DataFrame([{"Signal": name, "Value": f"{value:.0f}/100", "Weight": f"{weight}%"} for name, value, weight in components])
        st.dataframe(detail, width="stretch", hide_index=True, alt="Transparent components and weights for the available builder evidence score")
    st.caption(f"Observed listing records for this builder: {len(builder_rows)} · RERA IDs present: {int(rera.sum())}/{len(builder_rows)} · completed project sqft: not available in the connected schema.")
    st.caption("Coverage is limited to the currently connected catalogue. Listing status, RERA reference presence, and directory rank are not independent legal or delivery verification; no lifetime project or area claim is inferred.")

    if not api_key:
        st.info("Google review rating is not connected. Add GOOGLE_PLACES_API_KEY in Streamlit Cloud Secrets or the local environment to fetch a live rating on demand.")
        st.caption("Before enabling Google Places for public use, publish Terms of Use and a Privacy Policy that incorporate Google Maps Platform requirements.")
    else:
        st.caption("Fetching Google Places data can incur Google Maps Platform charges. Select the button above to make a one-time live request.")
    if google_error:
        st.error("Could not retrieve Google Places data.")
        st.caption(f"Connection error type: {google_error}.")
    elif google_place:
        place_name = google_place.get("displayName", {}).get("text", "Candidate")
        st.write(f"**Google Maps candidate:** {place_name} · {google_place.get('formattedAddress', 'address unavailable')}")
        rating = google_place.get("rating")
        review_count = google_place.get("userRatingCount")
        if rating is not None:
            st.metric("Google review signal", f"{float(rating):.1f}/5" + (f" · {int(review_count):,} ratings" if review_count is not None else ""), border=True)
            if google_exact:
                st.caption("Exact-name Google rating is included in the score for this view only. Google data is fetched live and not persisted.")
            else:
                st.warning("The Google candidate name differs from the project builder name. Its rating is shown separately and excluded from the score; verify the company identity before use.")
        else:
            st.info("This Google Maps candidate has no rating data.")
        if google_place.get("googleMapsUri"):
            st.link_button("Open Google Maps listing", google_place["googleMapsUri"])
        for attribution in google_place.get("attributions", []):
            attribution_name = attribution.get("provider", "Google Maps data provider")
            attribution_url = attribution.get("providerUri")
            st.caption(f"Google Maps attribution: {attribution_name}" + (f" · {attribution_url}" if attribution_url else ""))
        st.caption("Google Maps · rating and rating count fetched live for this view. Data is not stored. Verify the profile match before use.")


def show_source_catalog(db, market_slug: str | None = None, demo_city: str = "Bengaluru"):
    """Read-only live view of the real PropClue PostgreSQL and MongoDB stores."""
    st.subheader("Project listings and locality intelligence")
    st.caption("Live PropClue catalogue from PostgreSQL, with linked locality intelligence from MongoDB. This view is read-only.")
    refresh_col, _ = st.columns([1, 5])
    if refresh_col.button("Refresh source data", icon=":material/refresh:"):
        clear_source_cache()
        st.rerun()
    try:
        localities, listings = load_catalog()
    except Exception as exc:
        st.warning(
            "Live PropClue listings are unavailable from this Streamlit deployment. "
            "Showing the clearly labeled demo catalogue for now; Cloud needs a network-accessible PostgreSQL URL to load live records.",
            icon=":material/cloud_off:",
        )
        st.caption(postgres_source_diagnostic())
        st.caption(f"Connection detail ({type(exc).__name__}): {safe_connection_error(exc)}")
        demo_projects = db.scalars(
            select(Project).where(Project.city == demo_city).order_by(Project.name)
        ).all()
        demo_localities = db.scalars(
            select(Locality).where(Locality.city == demo_city).order_by(Locality.name)
        ).all()
        if not demo_projects:
            st.info(f"No demo project records are seeded for {demo_city}.")
            return
        st.markdown(f"#### {demo_city} · demo catalogue")
        st.caption("Synthetic demo data from the app database. These rows are not live PropClue PostgreSQL records.")
        metric_cols = st.columns(3)
        metric_cols[0].metric("Demo projects", f"{len(demo_projects):,}", border=True)
        metric_cols[1].metric("Demo localities", f"{len(demo_localities):,}", border=True)
        metric_cols[2].metric("Live source", "Unavailable", border=True)
        demo_rows = [{
            "Project": project.name,
            "Builder": project.builder.name if project.builder else "Not provided",
            "Type": project.property_type,
            "Locality": project.locality.name if project.locality else "Not provided",
            "Configuration": project.configuration,
            "Asking price": money(project.asking_price, compact=True),
            "Status": project.status,
            "Data type": "Synthetic demo",
        } for project in demo_projects]
        st.dataframe(pd.DataFrame(demo_rows), width="stretch", hide_index=True,
                     alt="Synthetic demo project catalogue shown while the live PostgreSQL source is unavailable")
        return
    if localities.empty or listings.empty:
        st.warning("The source connection succeeded, but no published localities or active listings were returned.")
        return

    locality_city_labels = {
        str(row.city_slug): str(row.city)
        for row in localities[["city_slug", "city"]].dropna().drop_duplicates().itertuples(index=False)
    }
    listing_cities = sorted(listings["city_slug"].dropna().astype(str).unique().tolist())
    served_markets = sorted(set(localities["city_slug"].dropna().astype(str)) | set(listing_cities))
    chosen_city_slug = market_slug if market_slug in served_markets else (served_markets[0] if served_markets else "")
    if not chosen_city_slug:
        st.warning("No live market records were found in PostgreSQL.")
        return
    city_label = locality_city_labels.get(chosen_city_slug, chosen_city_slug.replace("-", " ").title())
    city_listings = listings[listings["city_slug"] == chosen_city_slug].copy()
    city_localities = localities[localities["city_slug"] == chosen_city_slug].copy()
    locality_names = dict(zip(city_localities["slug"], city_localities["name"]))
    st.caption(f"Selected market: **{city_label}** · {chosen_city_slug}")
    if city_listings.empty:
        st.info("This market has published locality records but no active project listings in the connected PostgreSQL catalogue.")
    render_market_pulse(city_label, city_listings, city_localities)
    st.markdown("")

    st.info("These are the records currently stored in your PropClue databases. A source row or a populated field does not by itself mean that a price, document, score, or availability has been independently verified.", icon=":material/info:")
    st.markdown("#### Project listings · PostgreSQL")
    search = st.text_input("Search project, builder or locality", placeholder="Try a project or builder name", key="source-listing-search")
    if search.strip():
        query = search.strip().casefold()
        mask = city_listings[["project_name", "builder_name", "locality_slug", "address"]].fillna("").astype(str).apply(
            lambda column: column.str.casefold().str.contains(query, regex=False)
        ).any(axis=1)
        city_listings = city_listings[mask]
    display_listings = city_listings.copy()
    display_listings["Price range"] = display_listings.apply(
        lambda row: "–".join(money(v, True) for v in [row["price_min"], row["price_max"]] if pd.notna(v)) or "Not listed",
        axis=1,
    )
    display_listings["RERA"] = display_listings["rera_number"].replace("", pd.NA).fillna("Not provided")
    display_listings["Updated"] = pd.to_datetime(display_listings["updated_at"], errors="coerce").dt.strftime("%d %b %Y").fillna("—")
    display_listings["Locality"] = display_listings["locality_match_slug"].map(locality_names)
    display_listings["Locality"] = display_listings["Locality"].fillna(
        "Unmatched: " + display_listings["locality_slug"].fillna("not provided")
    )
    display_listings["Type"] = display_listings["project_type"].fillna("Not provided").str.replace("_", " ").str.title()
    display_listings["Construction status"] = display_listings["construction_status"].fillna("Not provided").str.replace("_", " ").str.title()
    display_listings["Bank approval"] = "Not verified"
    listing_columns = ["project_name", "builder_name", "Type", "Locality", "Price range", "units", "Construction status", "possession_date", "RERA", "Bank approval", "source", "data_confidence", "Updated"]
    listing_display = display_listings[listing_columns].rename(columns={
        "project_name": "Project", "builder_name": "Builder",
        "units": "Units stated", "construction_status": "Construction status", "possession_date": "Possession stated",
        "source": "Source", "data_confidence": "Source confidence",
    })
    listing_key_hash = hashlib.sha1("|".join(display_listings["id"].astype(str)).encode("utf-8")).hexdigest()[:10]
    listing_event = None
    selected_id = None
    listing = None
    if not display_listings.empty:
        listing_event = st.dataframe(listing_display.reset_index(drop=True), width="stretch", hide_index=True, height=380,
                 alt="Active PropClue project listing records read from PostgreSQL",
                 on_select="rerun", selection_mode="single-row",
                 selection_default={"selection": {"rows": [0]}}, key=f"live-listing-row-selection-{chosen_city_slug}-{listing_key_hash}")
        selected_rows = listing_event.selection.rows if hasattr(listing_event, "selection") else []
        selected_row = selected_rows[0] if selected_rows else 0
        selected_id = int(display_listings.reset_index(drop=True).iloc[selected_row]["id"])
        listing = display_listings.loc[display_listings["id"] == selected_id].iloc[0]
    else:
        st.info("No project listings match this search.")
    st.caption(f"Showing {len(display_listings):,} active database rows. Blank source fields are shown as missing; no unit area or per-square-foot price is inferred.")
    if listing is not None:
        st.caption("Select a row above to focus its details and locality intelligence.")
        with st.container(border=True):
            st.markdown(f"#### {listing['project_name']}")
            st.caption(f"{listing['builder_name'] or 'Builder not provided'} · {listing['Type']} · {listing['Locality']}")
            details = st.columns(4)
            details[0].metric("Stored price range", listing["Price range"], border=True)
            details[1].metric("Units stated", str(int(listing["units"])) if pd.notna(listing["units"]) else "Not provided", border=True)
            details[2].metric("Construction", listing["Construction status"], border=True)
            details[3].metric("RERA reference", str(listing["RERA"]), border=True)
            st.write(f"**Address:** {listing['address'] or 'Not provided'}")
            st.caption(f"Source: {listing['source'] or 'Not specified'} · confidence label: {listing['data_confidence'] or 'Not specified'} · last updated: {listing['Updated']}")
            links = st.columns(3)
            for col, label, field in zip(links, ["Project page", "Brochure", "Floor plan"], ["project_url", "brochure_url", "floor_plan_url"]):
                url = listing.get(field)
                if isinstance(url, str) and url.startswith("https://"):
                    col.link_button(label, url, width="stretch")
            matched_slug = str(listing.get("locality_match_slug") or "")
            listing_area = city_localities[city_localities["slug"].astype(str) == matched_slug]
            listing_doc = {}
            if matched_slug:
                try:
                    listing_doc = load_area_dna(matched_slug) or {}
                except Exception:
                    listing_doc = {}
            real_estate = listing_doc.get("real_estate", {}) if isinstance(listing_doc, dict) else {}
            if not listing_area.empty:
                area_row = listing_area.iloc[0]
                demo_pois = intelligence_demo_pois(float(area_row["latitude"]), float(area_row["longitude"]), 10, matched_slug)
                age = 1 + int(hashlib.sha256(f"listing-map:{int(listing['id'])}".encode()).hexdigest()[12:16], 16) % 35
                render_area_project_brief(
                    str(listing["project_name"]), area_row.get("lifestyle_score"), area_row.get("connectivity_score"),
                    age, demo_pois, scope_label="Area / Project Brief · final demo rating",
                )
            render_property_legal_and_returns(
                str(listing["project_name"]), listing.get("price_min"), listing.get("price_max"),
                real_estate.get("rental_yield") if isinstance(real_estate, dict) else None,
            )

    st.markdown("#### Locality intelligence · linked to selected project")
    preferred_slug = str(listing["locality_match_slug"]) if listing is not None and pd.notna(listing.get("locality_match_slug")) else None
    selected_project_name = str(listing["project_name"]) if listing is not None else None
    show_locality_intelligence(city_localities, city_listings, chosen_city_slug,
                               preferred_slug=preferred_slug, selected_project=selected_project_name)



def main():
    with SessionLocal() as db:
        user = current_user(db)
        if not user:
            login_screen(db)
            return
        with st.sidebar:
            brand_lockup(compact=True)
            st.caption(f"{user.name} · {user.role.title()}")
            if DATABASE_FALLBACK_NOTICE:
                st.warning(DATABASE_FALLBACK_NOTICE, icon=":material/cloud_off:")
            city = get_city_filter(db, user)
            if st.button("Sign out", width="stretch"):
                st.session_state.pop("user_id", None)
                st.rerun()
            st.markdown("---")
        page_groups = {
            "buyer": [
                st.Page(lambda: show_market_workspace(db, user, city), title="Market overview · Live data", url_path="market-overview", icon=":material/monitoring:"),
                st.Page(lambda: show_map(db, user, city), title="Map explorer", url_path="map-explorer", icon=":material/map:"),
                st.Page(lambda: show_compare_workspace(db, user), title="Compare & save", url_path="compare-save", icon=":material/bookmark:"),
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
                st.Page(lambda: show_market(db, city, user_role=user.role), title="Market intelligence", url_path="admin-market", icon=":material/trending_up:"),
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
        map_route = bool(re.search(r"/(?:map-explorer|admin-map)/?$", str(getattr(st.context, "url", ""))))
        st.markdown("""
        <style>
        div[data-testid="stPopover"] { position: fixed !important; display: inline-flex !important; width: max-content !important; max-width: calc(100vw - 1.5rem) !important; left: auto !important; right: 1.5rem !important; bottom: 1.25rem !important; z-index: 99999 !important; }
        div[data-testid="stPopover"] button, button[title="Open the PropClue property assistant"] { display: inline-flex !important; width: 220px !important; min-width: 220px !important; min-height: 64px !important; justify-content: center !important; border-radius: 999px !important; padding: .8rem 1.45rem !important; box-shadow: 0 10px 32px rgba(14, 116, 109, .38) !important; font-weight: 800 !important; font-size: 1.2rem !important; border: 2px solid rgba(255,255,255,.9) !important; }
        div[data-testid="stPopover"] button p, button[title="Open the PropClue property assistant"] p { font-size: 1.2rem !important; font-weight: 800 !important; }
        div[data-testid="stPopoverBody"] { width: min(410px, calc(100vw - 2rem)) !important; }
        @media (max-width: 640px) { div[data-testid="stPopover"] { right: .75rem !important; bottom: .75rem !important; } div[data-testid="stPopover"] button, button[title="Open the PropClue property assistant"] { width: 190px !important; min-width: 190px !important; min-height: 56px !important; font-size: 1.05rem !important; } }
        </style>
        """, unsafe_allow_html=True)
        if map_route:
            st.markdown("""
            <style>
            div[data-testid="stPopover"] { left:50% !important; right:auto !important; bottom:1rem !important; transform:translateX(-50%) !important; }
            div[data-testid="stPopover"] button, button[title="Open the PropClue property assistant"] { width:min(520px,92vw) !important; min-width:min(520px,92vw) !important; min-height:68px !important; border-radius:22px !important; background:linear-gradient(115deg,#0d9488,#087e8b) !important; }
            div[data-testid="stPopoverBody"] { width:min(660px,94vw) !important; }
            @media(max-width:640px){div[data-testid="stPopover"]{bottom:.6rem!important}div[data-testid="stPopover"] button{min-width:92vw!important}}
            </style>
            """, unsafe_allow_html=True)
        if not map_route:
            with st.popover("Ask PropClue · map, documents & investment", type="primary", help="Ask about property details, legal evidence, bank status, nearby POIs, or illustrative investment scenarios"):
                st.markdown("#### Property intelligence assistant")
                st.caption("Ask about legal/title checks, bank approval evidence, nearby schools, hospitals, tech parks, transit and other radius signals, or whether an investment scenario fits your assumptions.")
                show_assistant(db, user, city, with_header=False, floating=True)
        navigation.run()


main()
