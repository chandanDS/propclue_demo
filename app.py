from __future__ import annotations

import json
import os
import random
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from sqlalchemy import select, func

# Streamlit Cloud secrets are exposed before database.py creates its engine.
try:
    if "DATABASE_URL" in st.secrets and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
except Exception:
    pass

from database import (
    AIConversation, AIMessage, AuditLog, Builder, Campaign, ConsentRecord, DataSource,
    Event, Lead, Locality, MarketObservation, Project, ProjectComparison, SavedProperty,
    SessionLocal, User, init_db, reset_demo_data, score_from_events,
)


st.set_page_config(page_title="PropClue | Real estate intelligence", page_icon="⌂", layout="wide",
                   initial_sidebar_state="expanded")


@st.cache_resource
def boot_database() -> bool:
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
                      font=dict(family="DM Sans", color="#26384F"), legend_title_text="")
    fig.update_xaxes(showgrid=False, linecolor="#E6EBF1")
    fig.update_yaxes(gridcolor="#E9EEF2", zeroline=False)
    return fig


def current_user(db):
    user_id = st.session_state.get("user_id")
    return db.get(User, user_id) if user_id else None


def oidc_configured() -> bool:
    try:
        return bool(st.secrets.get("auth", {}))
    except Exception:
        return False


def login_screen(db):
    st.caption("PROPCLUE · REAL ESTATE INTELLIGENCE · INDIA")
    st.title("Understand the property behind the pin.")
    st.warning("Synthetic demo only. No verified property, legal, ownership, transaction, or investment information.")
    left, right = st.columns([1.05, .95], gap="large")
    with left:
        st.markdown("### A clearer view of the market")
        st.write("Explore synthetic project, locality, price, rental, and buyer-demand signals for Bengaluru and Delhi. Switch between buyer, builder, and platform-admin workspaces.")
        st.markdown("**Inside the demo**")
        st.write("Interactive map · 36-month market series · consent-aware leads · transparent buyer-intent scoring · simulated campaigns · data provenance")
        st.caption("Every displayed record is generated for demonstration. It is not suitable for legal, financial, lending, or investment decisions.")
    with right:
        with st.container(border=True):
            st.subheader("Choose a demo workspace")
            st.caption("This role switcher is for synthetic demo data. It is not authentication.")
            if oidc_configured():
                if not st.user.is_logged_in:
                    if st.button("Sign in with configured identity provider", type="primary", width="stretch"):
                        st.login()
                    st.stop()
                email = str(st.user.email or "").lower()
                try:
                    role = st.secrets.get("user_roles", {}).get(email)
                except Exception:
                    role = None
                if role not in {"buyer", "builder", "admin"}:
                    st.error("Your signed-in email is not mapped to a PropClue demo role. Configure `user_roles` in app secrets.")
                    st.stop()
                account_email = {"buyer": "demo.buyer@propclue.test", "builder": "demo.builder@propclue.test", "admin": "demo.admin@propclue.test"}[role]
                user = db.scalar(select(User).where(User.email == account_email))
                if not user:
                    st.error("The demo role account is unavailable.")
                    st.stop()
                st.session_state.user_id = user.id
                st.session_state.demo_session = st.session_state.get("demo_session", str(uuid.uuid4()))
                st.rerun()
            buttons = st.columns(3)
            for col, role, label, email in [
                (buttons[0], "buyer", "Buyer", "demo.buyer@propclue.test"),
                (buttons[1], "builder", "Builder", "demo.builder@propclue.test"),
                (buttons[2], "admin", "Admin", "demo.admin@propclue.test"),
            ]:
                if col.button(f"Enter as {label}", type="primary" if role == "buyer" else "secondary", width="stretch", key=f"demo-role-{role}"):
                    user = db.scalar(select(User).where(User.email == email))
                    if user:
                        st.session_state.user_id = user.id
                        st.session_state.demo_session = str(uuid.uuid4())
                        db.add(AuditLog(actor_id=user.id, action="demo_persona_selected", detail=role))
                        db.commit()
                        st.rerun()


def top_header(title: str, subtitle: str = ""):
    st.caption("PROPCLUE · SYNTHETIC MARKET INTELLIGENCE")
    st.title(title)
    if subtitle:
        st.caption(subtitle)
    st.warning("Synthetic, unverified demo data. Not suitable for legal, financial, lending, or investment decisions.")


def get_city_filter(db, user: User) -> str:
    choices = ["Bengaluru", "Delhi"]
    default = st.session_state.get("city_filter", user.city if user.city in choices else choices[0])
    selected = st.selectbox("Market", choices, index=choices.index(default) if default in choices else 0)
    st.session_state.city_filter = selected
    return selected


def show_overview(db, user: User, city: str, builder_id: int | None = None):
    top_header("Market pulse", f"A synthetic read of {city} demand, pricing and available inventory.")
    projects = db.scalars(select(Project).where(Project.city == city)).all()
    if builder_id:
        projects = [p for p in projects if p.builder_id == builder_id]
    loc_count = db.scalar(select(func.count(Locality.id)).where(Locality.city == city)) or 0
    project_ids = [p.id for p in projects]
    enquiries = db.scalar(select(func.count(Event.id)).where(Event.city == city, Event.event_type == "Enquiry submitted")) or 0
    leads = db.scalar(select(func.count(Lead.id)).where(Lead.builder_id == builder_id)) if builder_id else None
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Projects in view", f"{len(projects):,}", help="Synthetic projects generated for this demo.")
    c2.metric("Localities", f"{loc_count}", help="Bengaluru and Delhi coverage includes separate Delhi NCR markets.")
    c3.metric("Demo enquiries", f"{enquiries:,}", help="Synthetic event records; not actual buyer inquiries.")
    c4.metric("Portfolio leads" if builder_id else "Available inventory", f"{leads:,}" if builder_id and leads is not None else f"{sum(max(0, 100 - (i % 72)) for i, _ in enumerate(projects)):,}", help="Synthetic demo counts.")
    if not projects:
        st.info("No projects in this market yet.")
        return
    localities = db.scalars(select(Locality).where(Locality.city == city)).all()
    obs = db.scalars(select(MarketObservation).join(Locality).where(Locality.city == city).order_by(MarketObservation.month)).all()
    obs_df = pd.DataFrame([{"Month": x.month, "Locality": x.locality_id, "Asking price": x.asking_psf,
                            "Simulated transaction": x.transaction_psf, "Available units": x.available_units,
                            "Absorption %": x.absorption_pct, "Enquiries": x.enquiries} for x in obs])
    names = {loc.id: loc.name for loc in localities}
    obs_df["Locality"] = obs_df["Locality"].map(names)
    local_order = sorted(obs_df["Locality"].unique())
    selected_localities = st.multiselect("Compare localities", local_order, default=local_order[:4], key="overview_locs")
    filtered = obs_df[obs_df["Locality"].isin(selected_localities)] if selected_localities else obs_df
    left, right = st.columns([1.25, .75], gap="large")
    with left:
        if not filtered.empty:
            trend = filtered.melt(id_vars=["Month", "Locality"], value_vars=["Asking price", "Simulated transaction"],
                                  var_name="Series", value_name="₹ / sq ft")
            fig = px.line(trend, x="Month", y="₹ / sq ft", color="Locality", line_dash="Series",
                          title="Asking vs simulated transaction price")
            st.plotly_chart(chart_layout(fig), width="stretch", alt="36-month asking and simulated transaction price trends by locality", config={"displayModeBar": False})
    with right:
        city_leads = db.scalars(select(Lead).join(User, Lead.buyer_id == User.id).where(User.city == city)).all()
        intent = pd.Series([x.intent_category for x in city_leads]).value_counts().reindex(["High", "Medium", "Low"], fill_value=0).reset_index()
        intent.columns = ["Intent", "Buyers"]
        fig = px.pie(intent, names="Intent", values="Buyers", hole=.64,
                     color="Intent", color_discrete_map={"High": "#0E8A81", "Medium": "#E9A23B", "Low": "#AEBBC9"},
                     title="Buyer-intent mix")
        st.plotly_chart(chart_layout(fig), width="stretch", alt="Synthetic high, medium, and low buyer-intent mix", config={"displayModeBar": False})
    hot = sorted(localities, key=lambda x: x.locality_score, reverse=True)[:5]
    st.subheader("Locality snapshot")
    snap = pd.DataFrame([{"City scope": x.scope, "Locality": x.name, "Locality score": x.locality_score,
                          "Indicative asking ₹/sq ft": int(x.base_rate), "Demo rental yield": f"{x.rental_yield:.1f}%",
                          "Flood exposure demo score": x.flood_score} for x in hot])
    st.dataframe(snap, width="stretch", hide_index=True, alt="Top locality scores and synthetic rental and flood indicators")
    st.caption("Asking prices and simulated transaction prices remain separate series. All metrics use generated demo data.")


def show_discover(db, user: User, city: str):
    top_header("Discover projects", "Filter demo inventory, compare price signals and open a property intelligence profile.")
    city_localities = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    f1, f2, f3 = st.columns([1, 1, 1.25])
    locality = f1.selectbox("Locality", ["All localities"] + [x.name for x in city_localities])
    config = f2.selectbox("Configuration", ["All", "1 BHK", "2 BHK", "3 BHK", "4 BHK"])
    max_budget_cr = f3.slider("Max asking price (₹ Cr)", 0.5, 6.0, 3.0, 0.1)
    projects = get_projects(db, city, locality, config, int(max_budget_cr * 10_000_000))
    frame = project_frame(projects)
    st.caption(f"Showing {len(projects)} synthetic demo projects · Filter budget applies to asking price.")
    if frame.empty:
        st.warning("No projects match those filters. Try a higher budget or another locality.")
        return
    c1, c2 = st.columns([.9, 1.1], gap="large")
    with c1:
        st.dataframe(frame[["Project", "Locality", "Configuration", "Asking price", "Status"]],
                     width="stretch", hide_index=True, height=410,
                     column_config={"Asking price": st.column_config.NumberColumn(format="₹%.0f")},
                     alt="Filtered synthetic projects and asking prices")
        choices = {f"{p.name} · {p.locality.name} · {money(p.asking_price, True)}": p.id for p in projects}
        selected_label = st.selectbox("Open project profile", list(choices))
        selected = db.get(Project, choices[selected_label])
        if st.button("Open intelligence profile", type="primary", width="stretch"):
            st.session_state.selected_project = selected.id
            add_event(db, user.id, "Project viewed", selected, context={"configuration": selected.configuration, "budget_max": int(max_budget_cr * 10_000_000)})
            st.rerun()
    with c2:
        if projects:
            scatter = px.scatter(frame, x="Area (sq ft)", y="Asking price", color="Locality", size="Rental yield %",
                                 hover_name="Project", hover_data=["Configuration", "Status"],
                                 title="Demo inventory · asking price vs size")
        st.plotly_chart(chart_layout(scatter, height=405), width="stretch", alt="Synthetic project asking price compared with area and locality", config={"displayModeBar": False})
    profile_id = st.session_state.get("selected_project")
    profile = db.get(Project, profile_id) if profile_id else None
    if profile and profile.city == city:
        st.divider()
        show_project_profile(db, user, profile)


def show_project_profile(db, user: User, project: Project):
    loc = project.locality
    st.subheader(project.name)
    st.caption(f"{project.builder.name} · {project.city} / {loc.scope} · {loc.name} · {project.status} · Project ID {project.id}")
    with st.container(border=True):
        a, b, c, d = st.columns(4)
        a.metric("Indicative asking", money(project.asking_price, True))
        b.metric("Simulated transaction", money(project.transaction_price, True))
        c.metric("Area", f"{project.area_sqft:,} sq ft")
        d.metric("Demo monthly rent", money(project.monthly_rent))
        st.caption("Asking and simulated transaction values are distinct synthetic signals; neither is a verified market quote.")
        p1, p2, p3 = st.columns(3)
        p1.write(f"**Configuration:** {project.configuration}")
        p2.write(f"**Building:** {project.tower_count} towers · {project.floors} floors")
        p3.write(f"**Possession:** {project.possession} (synthetic)")
        st.write("**Amenities:** " + " · ".join(json.loads(project.amenities_json)))
        st.write(f"**Locality score:** {loc.locality_score}/100 · **Rental yield demo:** {loc.rental_yield:.1f}% · **Flood exposure demo score:** {loc.flood_score}/100")
        st.warning("Legal due diligence: Synthetic demo record. No official title, RERA, approval, ownership, encumbrance or litigation checks are connected.")
        st.caption(f"Data provenance: Synthetic demo data · generated {date.today().isoformat()} · unverified · confidence: illustrative only")
        b1, b2, b3 = st.columns(3)
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
        if b3.button("Record enquiry (demo)", key=f"enquiry-{project.id}"):
            add_event(db, user.id, "Enquiry submitted", project)
            st.success("Demo enquiry recorded locally. No builder was contacted.")
        obs = db.scalars(select(MarketObservation).where(MarketObservation.locality_id == loc.id).order_by(MarketObservation.month)).all()
        series = pd.DataFrame([{"Month": x.month, "Asking price": x.asking_psf, "Simulated transaction": x.transaction_psf} for x in obs])
        fig = px.line(series, x="Month", y=["Asking price", "Simulated transaction"], title="36-month locality series · ₹ per sq ft")
        st.plotly_chart(chart_layout(fig, height=300), width="stretch", alt="36-month asking and simulated transaction price trend for this locality", config={"displayModeBar": False})


def show_map(db, user: User, city: str):
    top_header("Map explorer", "Pan and zoom through synthetic project points and demonstration infrastructure layers.")
    locs = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    projects = db.scalars(select(Project).where(Project.city == city)).all()
    c1, c2, c3 = st.columns([1.1, 1, 1])
    selected_loc = c1.selectbox("Focus locality", [x.name for x in locs])
    show_poi = c2.toggle("Employment & transit demo points", value=True)
    show_areas = c3.toggle("Locality coverage circles", value=True)
    selected = next((x for x in locs if x.name == selected_loc), locs[0])
    layers = []
    if show_areas:
        polys = []
        for loc in locs:
            lat, lon = loc.latitude, loc.longitude
            d = .018
            polys.append({"name": loc.name, "score": loc.locality_score,
                          "polygon": [[lon-d, lat-d], [lon+d, lat-d], [lon+d, lat+d], [lon-d, lat+d], [lon-d, lat-d]]})
        layers.append(pdk.Layer("PolygonLayer", data=polys, get_polygon="polygon", get_fill_color="[18, 138, 129, 18]",
                                get_line_color="[14, 138, 129, 150]", line_width_min_pixels=1, pickable=True, stroked=True, filled=True))
    project_data = [{"name": x.name, "locality": x.locality.name, "config": x.configuration,
                     "price": money(x.asking_price, True), "coordinates": [x.longitude, x.latitude],
                     "asking_price": x.asking_price} for x in projects]
    layers.append(pdk.Layer("ScatterplotLayer", data=project_data, get_position="coordinates", get_radius=90,
                            get_fill_color="[14, 138, 129, 210]", get_line_color="[255,255,255,230]",
                            line_width_min_pixels=1, pickable=True, opacity=.82))
    if show_poi:
        poi_data = []
        for i, loc in enumerate(locs):
            poi_data.extend([
                {"name": "Synthetic employment hub", "coordinates": [loc.longitude + .009, loc.latitude + .008], "type": "Employment"},
                {"name": "Synthetic transit node", "coordinates": [loc.longitude - .008, loc.latitude - .006], "type": "Transit"},
            ])
        layers.append(pdk.Layer("ScatterplotLayer", data=poi_data, get_position="coordinates", get_radius=65,
                                get_fill_color="[236, 165, 66, 210]", pickable=True, opacity=.82))
    map_style = os.environ.get("PROPCLUE_MAP_STYLE", "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json")
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=selected.latitude, longitude=selected.longitude, zoom=10.6, pitch=20),
                    map_style=map_style, tooltip={"html": "<b>{name}</b><br/>{locality}<br/>{config}<br/>{price}", "style": {"color": "white"}})
    st.pydeck_chart(deck, width="stretch", height=510, key=f"map-{city}-{selected_loc}", alt=f"Synthetic {city} project and locality map centered on {selected.name}")
    left, right = st.columns([1, 1])
    with left:
        st.markdown(f"### {selected.name} · {selected.scope}")
        st.write(f"Locality intelligence score **{selected.locality_score}/100** · indicative base asking **{money(selected.base_rate)}/sq ft** · demo rental yield **{selected.rental_yield:.1f}%**")
        st.caption("The translucent locality shapes are illustrative coverage areas, not surveyed locality or parcel boundaries.")
    with right:
        if st.button("Record locality exploration", type="primary"):
            add_event(db, user.id, "Map area searched", locality=selected.name, context={"city": city, "scope": selected.scope})
            st.success("Demo map-search event recorded.")


def show_market(db, city: str):
    top_header("Market intelligence", "Track asking prices, simulated transactions, rental indicators and supply independently.")
    locs = db.scalars(select(Locality).where(Locality.city == city).order_by(Locality.name)).all()
    chosen = st.multiselect("Localities", [x.name for x in locs], default=[x.name for x in locs[:5]], key="market_locs")
    rows = db.scalars(select(MarketObservation).join(Locality).where(Locality.city == city).order_by(MarketObservation.month)).all()
    loc_name = {x.id: x.name for x in locs}
    data = pd.DataFrame([{"Month": r.month, "Locality": loc_name[r.locality_id], "Asking ₹/sq ft": r.asking_psf,
                          "Simulated transaction ₹/sq ft": r.transaction_psf, "Rent ₹/sq ft": r.rent_psf,
                          "Available units": r.available_units, "Absorption %": r.absorption_pct,
                          "Buyer enquiries": r.enquiries} for r in rows])
    if chosen:
        data = data[data["Locality"].isin(chosen)]
    m1, m2, m3 = st.columns(3)
    m1.metric("Average asking price", money(data["Asking ₹/sq ft"].mean()) + "/sq ft")
    m2.metric("Avg. simulated transaction", money(data["Simulated transaction ₹/sq ft"].mean()) + "/sq ft")
    m3.metric("Demo monthly absorption", f"{data['Absorption %'].mean():.1f}%")
    p1, p2 = st.columns(2, gap="large")
    with p1:
        price = data.melt(id_vars=["Month", "Locality"], value_vars=["Asking ₹/sq ft", "Simulated transaction ₹/sq ft"], var_name="Series", value_name="₹ / sq ft")
        fig = px.line(price, x="Month", y="₹ / sq ft", color="Locality", line_dash="Series", title="Asking and simulated transaction prices")
        st.plotly_chart(chart_layout(fig), width="stretch", alt="Asking price and simulated transaction price by locality over 36 months", config={"displayModeBar": False})
    with p2:
        supply = data.groupby(["Locality"], as_index=False).agg({"Available units": "mean", "Buyer enquiries": "mean"})
        supply = supply.melt(id_vars="Locality", var_name="Signal", value_name="Monthly average")
        fig = px.bar(supply, x="Locality", y="Monthly average", color="Signal", barmode="group", title="Supply vs buyer enquiries · demo")
        st.plotly_chart(chart_layout(fig), width="stretch", alt="Synthetic available units compared with buyer inquiries across localities", config={"displayModeBar": False})
    yields = pd.DataFrame([{"Locality": x.name, "Demo rental yield %": x.rental_yield, "Locality score": x.locality_score,
                            "Flood exposure demo score": x.flood_score} for x in locs if not chosen or x.name in chosen])
    fig = px.scatter(yields, x="Locality score", y="Demo rental yield %", size="Flood exposure demo score", color="Locality",
                     hover_data=["Flood exposure demo score"], title="Locality indicators · synthetic")
    st.plotly_chart(chart_layout(fig, height=290), width="stretch", alt="Synthetic locality score and rental yield with flood exposure size", config={"displayModeBar": False})
    st.caption("The 36-month series is generated deterministically. Transaction and asking price fields are never combined.")


def builder_leads(db, builder_id: int, city: str | None = None):
    stmt = select(Lead, User, Project).join(User, Lead.buyer_id == User.id).join(Project, Lead.project_id == Project.id).where(Lead.builder_id == builder_id)
    rows = db.execute(stmt).all()
    consent_map = latest_consents(db, [buyer.id for _, buyer, _ in rows])
    records = []
    for lead, buyer, project in rows:
        if city and buyer.city != city:
            continue
        records.append({"Lead": lead, "Buyer": buyer, "Project record": project,
                        "Buyer name": buyer.name, "City": buyer.city, "Locality": buyer.locality,
                        "Budget": f"{money(buyer.budget_min, True)}–{money(buyer.budget_max, True)}",
                        "Configuration": buyer.configuration, "Timeline": buyer.timeline,
                        "Project": project.name, "Intent score": lead.intent_score,
                        "Intent": lead.intent_category, "Last activity": lead.last_activity.strftime("%d %b %Y"),
                        "Marketing eligible": consent_map.get((buyer.id, "builder_marketing"), False), "Status": lead.status,
                        "Next action": "Offer a site visit" if lead.intent_category == "High" else "Share project details"})
    return records


def show_leads(db, user: User, city: str):
    builder = db.get(Builder, user.builder_id)
    top_header("Buyer demand & leads", f"{builder.name if builder else 'Your portfolio'} · only leads assigned to your demo builder account are shown.")
    records = builder_leads(db, user.builder_id, city)
    if not records:
        st.info("No leads match this city and your authorized portfolio.")
        return
    f1, f2, f3 = st.columns(3)
    intents = f1.multiselect("Intent", ["High", "Medium", "Low"], default=["High", "Medium"])
    localities = sorted({r["Locality"] for r in records})
    selected_loc = f2.selectbox("Locality", ["All"] + localities)
    statuses = sorted({r["Status"] for r in records})
    selected_status = f3.selectbox("Lead status", ["All"] + statuses)
    filtered = [r for r in records if r["Intent"] in intents and (selected_loc == "All" or r["Locality"] == selected_loc) and (selected_status == "All" or r["Status"] == selected_status)]
    high = sum(r["Intent"] == "High" for r in filtered)
    opted = sum(r["Marketing eligible"] for r in filtered)
    c1, c2, c3 = st.columns(3)
    c1.metric("Authorized leads", len(filtered))
    c2.metric("High intent", high)
    c3.metric("Eligible for builder marketing", opted, help="Eligibility reflects the latest synthetic builder-marketing consent record.")
    display = pd.DataFrame([{k: r[k] for k in ["Buyer name", "City", "Locality", "Budget", "Configuration", "Timeline", "Project", "Intent score", "Intent", "Last activity", "Marketing eligible", "Status", "Next action"]} for r in filtered])
    st.dataframe(display, width="stretch", hide_index=True, height=420, alt="Builder-authorized synthetic leads with consent and intent fields")
    st.caption("Only consent-eligible records may enter a simulated outreach audience. Demo data does not represent real buyers.")
    names = {f"{r['Buyer name']} · {r['Locality']} · score {r['Intent score']}": r for r in filtered}
    if names:
        chosen = st.selectbox("Explain a buyer-intent score", list(names))
        record = names[chosen]
        with st.container(border=True):
            st.markdown(f"### Why this demo buyer is {record['Intent'].lower()} intent")
            events = db.scalars(select(Event).where(Event.buyer_id == record["Buyer"].id).order_by(Event.event_at.desc()).limit(60)).all()
            score, category, reasons = score_from_events(events)
            st.write(f"Current model score: **{score}/100 · {category} intent**. Recent activity decays by half every 30 days; scores cap at 100.")
            for reason in reasons:
                st.write(f"• {reason}")
            st.caption("Scoring uses only demo engagement events, property preferences and recency. It does not use protected or sensitive characteristics.")
    with st.expander("Export consent-eligible leads"):
        eligible = [r for r in filtered if r["Marketing eligible"]]
        if eligible:
            export = pd.DataFrame([{k: r[k] for k in ["Buyer name", "City", "Locality", "Budget", "Configuration", "Timeline", "Project", "Intent score", "Intent", "Status"]} for r in eligible])
            st.download_button("Download eligible demo leads CSV", export.to_csv(index=False), "propclue-eligible-demo-leads.csv", "text/csv")
        else:
            st.info("No buyers in this view have builder-marketing consent.")


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
    q = question.lower()
    locality_rows = db.scalars(select(Locality).where(Locality.city == city)).all()
    observations = db.scalars(select(MarketObservation).join(Locality).where(Locality.city == city)).all()
    demand: dict[int, int] = {}
    for row in observations:
        demand[row.locality_id] = demand.get(row.locality_id, 0) + row.enquiries
    by_name = {x.id: x for x in locality_rows}
    leaders = sorted(demand.items(), key=lambda item: item[1], reverse=True)[:3]
    if "high intent" in q or "buyer" in q or "lead" in q:
        rows = builder_leads(db, user.builder_id, city)
        rows = sorted(rows, key=lambda row: row["Intent score"], reverse=True)[:5]
        if not rows:
            return "Your demo builder account has no authorized leads in this market. Try the other city filter."
        names = ", ".join(f"{r['Buyer name']} ({r['Intent score']}/100, {r['Locality']})" for r in rows)
        return f"Your top authorized demo buyers in {city} are {names}. The table is restricted to your builder portfolio and the lead scores use synthetic engagement only."
    if "why" in q:
        return "The buyer-intent model applies configurable demo weights to enquiries (+30), contact requests (+25), shortlists (+15), comparisons (+10), report downloads (+10), repeat views (+8), and recent activity. Each signal decays by half every 30 days. Sensitive attributes are excluded."
    if "whitefield" in q or "sarjapur" in q or "dwarka" in q or "saket" in q:
        selected_loc = next((x for x in locality_rows if x.name.lower() in q), None)
        if selected_loc:
            count = db.scalar(select(func.count(Project.id)).where(Project.locality_id == selected_loc.id)) or 0
            return f"{selected_loc.name} has {count} synthetic project records, a demo locality score of {selected_loc.locality_score}/100, and indicative asking prices around {money(selected_loc.base_rate)}/sq ft. The demand series is generated data, not a verified market signal."
    tops = ", ".join(f"{by_name[i].name} ({n:,} demo inquiries)" for i, n in leaders)
    project = db.scalar(select(Project).where(Project.city == city).order_by(Project.asking_price.desc()))
    if "project" in q or "inventory" in q:
        own_projects = db.scalars(select(Project).where(Project.builder_id == user.builder_id, Project.city == city)).all()
        own_ids = [p.id for p in own_projects]
        if not own_projects:
            return f"No projects are currently assigned to your builder in {city}."
        return f"Your highest asking-price project in {city} is {max(own_projects, key=lambda p: p.asking_price).name}. Across the demo market, locality enquiry leaders are {tops}. These are synthetic observations."
    if "whatsapp" in q or "message" in q or "draft" in q:
        return "Draft only: ‘Hello, based on your stated project preferences, we have an update you may find useful. Would you like us to share more details?’ This message was not sent. Contact should only happen after real consent and authorization."
    return f"For {city}, the highest demo enquiry volumes are {tops}. Asking prices are stored separately from simulated transaction prices. Ask about a locality, portfolio projects, or high-intent leads for a more focused answer."


def show_assistant(db, user: User, city: str):
    top_header("PropClue AI assistant", "Deterministic, database-backed answers for this demo. No external LLM key is used.")
    st.caption("Answers query the synthetic records in the database. Draft messages remain unsent.")
    if "conversation_id" not in st.session_state:
        conversation = AIConversation(user_id=user.id, title="Demo market questions")
        db.add(conversation)
        db.commit()
        st.session_state.conversation_id = conversation.id
    cid = st.session_state.conversation_id
    messages = db.scalars(select(AIMessage).where(AIMessage.conversation_id == cid).order_by(AIMessage.created_at)).all()
    for message in messages[-20:]:
        with st.chat_message(message.role):
            st.markdown(message.content)
    question = st.chat_input("Ask about demand, projects, leads or score explanations")
    if question:
        answer = assistant_answer(db, user, city, question)
        db.add_all([AIMessage(conversation_id=cid, role="user", content=question), AIMessage(conversation_id=cid, role="assistant", content=answer)])
        db.commit()
        st.rerun()
    with st.expander("Try a prompt"):
        st.write("Which of my projects has the highest demand? · Show high-intent buyers in Bengaluru · Why are buyers high intent? · Prepare a WhatsApp draft")


def show_activity(db, user: User):
    top_header("My activity", "A timeline of demo events attached to your buyer account.")
    rows = db.scalars(select(Event).where(Event.buyer_id == user.id).order_by(Event.event_at.desc()).limit(80)).all()
    st.metric("Recorded demo activities", db.scalar(select(func.count(Event.id)).where(Event.buyer_id == user.id)) or 0)
    if rows:
        st.dataframe(pd.DataFrame([{"When": r.event_at.strftime("%d %b %Y · %H:%M"), "Event": r.event_type,
                                    "City": r.city, "Locality": r.locality,
                                    "Project": db.get(Project, r.project_id).name if r.project_id and db.get(Project, r.project_id) else "—",
                                    "Consent processing": r.consent_status} for r in rows]), width="stretch", hide_index=True, alt="Recent recorded buyer activity in the demo account")
    else:
        st.info("Your demo activity timeline is empty. Explore or save a project to add an event.")


def show_saved(db, user: User):
    top_header("Compare & saved", "Review the projects you saved and compare up to four synthetic records.")
    saved = db.scalars(select(SavedProperty).where(SavedProperty.user_id == user.id).order_by(SavedProperty.saved_at.desc())).all()
    saved_projects = [db.get(Project, x.project_id) for x in saved if db.get(Project, x.project_id)]
    if saved_projects:
        st.dataframe(project_frame(saved_projects).drop(columns=["Project ID"]), width="stretch", hide_index=True, alt="Saved synthetic projects and market indicators")
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
                                {"Measure": "Locality score", **{p.name: p.locality.locality_score for p in comps}},
                                {"Measure": "Demo rental yield", **{p.name: f"{p.monthly_rent * 12 / p.asking_price * 100:.1f}%" for p in comps}},
                                {"Measure": "Verification", **{p.name: "Synthetic demo record" for p in comps}}])
        st.dataframe(compare, width="stretch", hide_index=True, alt="Side-by-side comparison of synthetic projects")
        if st.button("Save comparison history", type="primary"):
            db.add(ProjectComparison(user_id=user.id, project_ids_json=json.dumps([p.id for p in comps])))
            for project in comps:
                add_event(db, user.id, "Property compared", project)
            db.commit()
            st.success("Comparison saved to your demo account.")


def show_privacy(db, user: User):
    top_header("Privacy & consent", "Review purpose-specific demo preferences. Every change is stored as a timestamped consent record.")
    current = latest_consents(db, [user.id])
    purposes = {"analytics": "Product analytics", "personalization": "Personalized discovery", "builder_marketing": "Builder contact and marketing"}
    st.info("This demo uses synthetic buyer profiles and activity. In production, consent and access must be reviewed with legal counsel and compliant data controls.")
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
        boot_database.clear()
        st.success("Demo data reset. Sign in again to continue.")
        st.rerun()


def main():
    with SessionLocal() as db:
        auth_configured = oidc_configured()
        if auth_configured:
            if st.user.is_logged_in:
                email = str(st.user.email or "").lower()
                try:
                    role = st.secrets.get("user_roles", {}).get(email)
                except Exception:
                    role = None
                if role in {"buyer", "builder", "admin"}:
                    account_email = {"buyer": "demo.buyer@propclue.test", "builder": "demo.builder@propclue.test", "admin": "demo.admin@propclue.test"}[role]
                    mapped_user = db.scalar(select(User).where(User.email == account_email))
                    if mapped_user:
                        st.session_state.user_id = mapped_user.id
                else:
                    st.session_state.pop("user_id", None)
            else:
                st.session_state.pop("user_id", None)
        user = current_user(db)
        if not user:
            login_screen(db)
            return
        with st.sidebar:
            st.title(":material/other_houses: PropClue")
            st.caption(f"{user.name} · {user.role.title()}")
            city = get_city_filter(db, user)
            if st.button("Sign out", width="stretch"):
                st.session_state.pop("user_id", None)
                if auth_configured:
                    st.logout()
                st.rerun()
            st.markdown("---")
            st.caption("SYNTHETIC DEMO · not verified")
        page_groups = {
            "buyer": [
                st.Page(lambda: show_overview(db, user, city), title="Market pulse", url_path="market-pulse", icon=":material/monitoring:"),
                st.Page(lambda: show_discover(db, user, city), title="Discover projects", url_path="discover-projects", icon=":material/search:"),
                st.Page(lambda: show_map(db, user, city), title="Map explorer", url_path="buyer-map", icon=":material/map:"),
                st.Page(lambda: show_market(db, city), title="Market intelligence", url_path="buyer-market", icon=":material/trending_up:"),
                st.Page(lambda: show_saved(db, user), title="Compare & saved", url_path="compare-saved", icon=":material/compare_arrows:"),
                st.Page(lambda: show_activity(db, user), title="My activity", url_path="my-activity", icon=":material/history:"),
                st.Page(lambda: show_privacy(db, user), title="Privacy & consent", url_path="privacy-consent", icon=":material/privacy_tip:"),
            ],
            "builder": [
                st.Page(lambda: show_overview(db, user, city, user.builder_id), title="Portfolio overview", url_path="portfolio-overview", icon=":material/monitoring:"),
                st.Page(lambda: show_market(db, city), title="Market intelligence", url_path="builder-market", icon=":material/trending_up:"),
                st.Page(lambda: show_leads(db, user, city), title="Buyer demand & leads", url_path="buyer-demand-leads", icon=":material/group:"),
                st.Page(lambda: show_segments(db, user, city), title="Segments & campaigns", url_path="segments-campaigns", icon=":material/campaign:"),
                st.Page(lambda: show_assistant(db, user, city), title="PropClue AI assistant", url_path="ai-assistant", icon=":material/smart_toy:"),
            ],
            "admin": [
                st.Page(lambda: show_admin(db, user), title="Platform overview", url_path="platform-overview", icon=":material/space_dashboard:"),
                st.Page(lambda: show_coverage(db), title="Data coverage & licensing", url_path="data-coverage-licensing", icon=":material/database:"),
                st.Page(lambda: show_map(db, user, city), title="Map explorer", url_path="admin-map", icon=":material/map:"),
                st.Page(lambda: show_market(db, city), title="Market intelligence", url_path="admin-market", icon=":material/trending_up:"),
            ],
        }
        navigation = st.navigation({user.role.title(): page_groups.get(user.role, page_groups["buyer"])}, position="sidebar")
        navigation.run()


main()
