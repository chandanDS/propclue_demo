"""Demo database, deterministic seed data, and explainable buyer scoring."""
from __future__ import annotations

import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, select, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if value:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value
    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(exist_ok=True)
    return f"sqlite:///{data_dir / 'propclue_demo.db'}"


DB_URL = _database_url()
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Builder(Base):
    __tablename__ = "builders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    city: Mapped[str] = mapped_column(String(40))
    segment: Mapped[str] = mapped_column(String(40))


class Locality(Base):
    __tablename__ = "localities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(40), index=True)
    scope: Mapped[str] = mapped_column(String(20), default="City")
    name: Mapped[str] = mapped_column(String(100), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    base_rate: Mapped[float] = mapped_column(Float)
    locality_score: Mapped[int] = mapped_column(Integer)
    rental_yield: Mapped[float] = mapped_column(Float)
    flood_score: Mapped[int] = mapped_column(Integer)
    provenance: Mapped[str] = mapped_column(String(80), default="Synthetic demo data")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    city: Mapped[str] = mapped_column(String(40), default="Bengaluru")
    locality: Mapped[str] = mapped_column(String(100), default="Whitefield")
    budget_min: Mapped[int] = mapped_column(Integer, default=10000000)
    budget_max: Mapped[int] = mapped_column(Integer, default=20000000)
    configuration: Mapped[str] = mapped_column(String(30), default="2 BHK")
    timeline: Mapped[str] = mapped_column(String(40), default="Within 6 months")
    builder_id: Mapped[int | None] = mapped_column(ForeignKey("builders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelPartnerProfile(Base):
    __tablename__ = "channel_partner_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(120), default="Independent partner")
    builder_id: Mapped[int | None] = mapped_column(ForeignKey("builders.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="Pending approval", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    builder_id: Mapped[int] = mapped_column(ForeignKey("builders.id"), index=True)
    locality_id: Mapped[int] = mapped_column(ForeignKey("localities.id"), index=True)
    name: Mapped[str] = mapped_column(String(140), index=True)
    city: Mapped[str] = mapped_column(String(40), index=True)
    property_type: Mapped[str] = mapped_column(String(30), default="Apartment")
    configuration: Mapped[str] = mapped_column(String(30))
    area_sqft: Mapped[int] = mapped_column(Integer)
    asking_price: Mapped[int] = mapped_column(Integer)
    transaction_price: Mapped[int] = mapped_column(Integer)
    monthly_rent: Mapped[int] = mapped_column(Integer)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    tower_count: Mapped[int] = mapped_column(Integer, default=2)
    floors: Mapped[int] = mapped_column(Integer, default=18)
    launch_year: Mapped[int] = mapped_column(Integer)
    possession: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    amenities_json: Mapped[str] = mapped_column(Text, default="[]")
    legal_status: Mapped[str] = mapped_column(String(60), default="Synthetic demo record")
    locality: Mapped[Locality] = relationship()
    builder: Mapped[Builder] = relationship()


class MarketObservation(Base):
    __tablename__ = "market_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    locality_id: Mapped[int] = mapped_column(ForeignKey("localities.id"), index=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    asking_psf: Mapped[float] = mapped_column(Float)
    transaction_psf: Mapped[float] = mapped_column(Float)
    rent_psf: Mapped[float] = mapped_column(Float)
    available_units: Mapped[int] = mapped_column(Integer)
    absorption_pct: Mapped[float] = mapped_column(Float)
    enquiries: Mapped[int] = mapped_column(Integer)


class PropertyTransaction(Base):
    __tablename__ = "property_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    locality_id: Mapped[int] = mapped_column(ForeignKey("localities.id"), index=True)
    observed_on: Mapped[date] = mapped_column(Date, index=True)
    transaction_type: Mapped[str] = mapped_column(String(30))
    configuration: Mapped[str] = mapped_column(String(30))
    area_sqft: Mapped[int] = mapped_column(Integer)
    price_inr: Mapped[int] = mapped_column(Integer)
    provenance: Mapped[str] = mapped_column(String(80), default="Synthetic demo data")


class RentalTransaction(Base):
    __tablename__ = "rental_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    locality_id: Mapped[int] = mapped_column(ForeignKey("localities.id"), index=True)
    observed_on: Mapped[date] = mapped_column(Date, index=True)
    configuration: Mapped[str] = mapped_column(String(30))
    area_sqft: Mapped[int] = mapped_column(Integer)
    monthly_rent_inr: Mapped[int] = mapped_column(Integer)
    vacancy_days: Mapped[int] = mapped_column(Integer)
    provenance: Mapped[str] = mapped_column(String(80), default="Synthetic demo data")


class Event(Base):
    __tablename__ = "user_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    city: Mapped[str] = mapped_column(String(40), index=True)
    locality: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    event_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    session_id: Mapped[str] = mapped_column(String(60))
    source_channel: Mapped[str] = mapped_column(String(40), default="demo web")
    consent_status: Mapped[str] = mapped_column(String(30), default="analytics granted")
    context_json: Mapped[str] = mapped_column(Text, default="{}")


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    granted: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(60), default="Demo preference center")


class SavedProperty(Base):
    __tablename__ = "saved_properties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectComparison(Base):
    __tablename__ = "project_comparisons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    project_ids_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    builder_id: Mapped[int] = mapped_column(ForeignKey("builders.id"), index=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="New")
    intent_score: Mapped[int] = mapped_column(Integer, default=0)
    intent_category: Mapped[str] = mapped_column(String(20), default="Low")
    last_activity: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    assigned_to: Mapped[str] = mapped_column(String(80), default="Demo sales team")


class LeadActivity(Base):
    """Auditable workflow updates for buyer-submitted enquiries and demo leads."""
    __tablename__ = "lead_activities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    status_after: Mapped[str] = mapped_column(String(40), default="New")
    note: Mapped[str] = mapped_column(Text, default="")
    contact_channel: Mapped[str] = mapped_column(String(30), default="")
    share_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_documents_json: Mapped[str] = mapped_column(Text, default="[]")
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    appointment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ProjectInventory(Base):
    """Unit-level inventory placeholder with explicit provenance and freshness."""
    __tablename__ = "project_inventory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    unit_label: Mapped[str] = mapped_column(String(40))
    availability: Mapped[str] = mapped_column(String(20), default="Available")
    asking_price: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str] = mapped_column(String(100), default="Synthetic demo seed")
    verification_status: Mapped[str] = mapped_column(String(40), default="Synthetic · unverified")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectEvidence(Base):
    """Document/approval evidence registry. Records stay unverified without a source."""
    __tablename__ = "project_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), default="Not connected")
    source_name: Mapped[str] = mapped_column(String(100), default="No connected provider")
    source_reference: Mapped[str] = mapped_column(String(180), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(40), default="Unverified")


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    builder_id: Mapped[int] = mapped_column(ForeignKey("builders.id"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    segment: Mapped[str] = mapped_column(String(120))
    audience_size: Mapped[int] = mapped_column(Integer)
    eligible_count: Mapped[int] = mapped_column(Integer)
    simulated_clicks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="Draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(60))
    source_name: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(40))
    coverage: Mapped[str] = mapped_column(String(100))
    licensing_status: Mapped[str] = mapped_column(String(50))
    verification_status: Mapped[str] = mapped_column(String(60))
    priority: Mapped[str] = mapped_column(String(30))
    demo_substitute: Mapped[str] = mapped_column(String(140))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIConversation(Base):
    __tablename__ = "ai_conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="Demo analysis")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIMessage(Base):
    __tablename__ = "ai_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("ai_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def score_from_events(events: list[Event]) -> tuple[int, str, list[str]]:
    weights = {
        "Enquiry submitted": 30, "Contact request": 25, "Project shortlisted": 15,
        "Property compared": 10, "Report downloaded": 10, "Repeat project view": 8,
        "Property viewed": 3, "Project viewed": 3, "Locality viewed": 2,
        "Map area searched": 2, "Price filter applied": 2,
    }
    now = datetime.utcnow()
    total = 0.0
    reasons: dict[str, float] = {}
    recent: int = 0
    for event in events:
        days = max(0, (now - event.event_at).days)
        decay = 0.5 ** (days / 30)
        contribution = weights.get(event.event_type, 0) * decay
        total += contribution
        if contribution >= 3:
            reasons[event.event_type] = reasons.get(event.event_type, 0) + contribution
        if days <= 7:
            recent += 1
    recency_bonus = min(8, recent * 1.5)
    score = min(100, int(round(total + recency_bonus)))
    if score >= 75:
        category = "High"
    elif score >= 45:
        category = "Medium"
    else:
        category = "Low"
    details = [f"{name}: +{int(value)}" for name, value in sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:4]]
    if recent:
        details.append(f"Recent activity in the last 7 days: +{int(recency_bonus)}")
    return score, category, details or ["No recent high-intent activity"]


LOCALITIES: list[tuple[str, str, str, float, float, float, int, float, int]] = [
    ("Bengaluru", "City", "Whitefield", 12.9698, 77.7500, 11200, 82, 3.6, 38),
    ("Bengaluru", "City", "Varthur", 12.9400, 77.7460, 9800, 75, 3.8, 46),
    ("Bengaluru", "City", "Sarjapur Road", 12.9010, 77.6800, 10300, 79, 3.9, 42),
    ("Bengaluru", "City", "Bellandur", 12.9304, 77.6784, 12500, 84, 3.5, 55),
    ("Bengaluru", "City", "Marathahalli", 12.9591, 77.6974, 11800, 80, 3.7, 40),
    ("Bengaluru", "City", "Outer Ring Road", 12.9352, 77.6880, 13800, 88, 3.2, 43),
    ("Bengaluru", "City", "Electronic City", 12.8452, 77.6602, 7600, 73, 4.1, 34),
    ("Bengaluru", "City", "HSR Layout", 12.9116, 77.6389, 13200, 91, 3.4, 31),
    ("Bengaluru", "City", "Indiranagar", 12.9784, 77.6408, 17400, 94, 3.0, 24),
    ("Bengaluru", "City", "Hebbal", 13.0358, 77.5970, 10400, 81, 3.7, 37),
    ("Bengaluru", "City", "Yelahanka", 13.1007, 77.5963, 7400, 70, 4.0, 29),
    ("Bengaluru", "City", "Devanahalli", 13.2470, 77.7130, 5200, 64, 4.2, 22),
    ("Bengaluru", "City", "Thanisandra", 13.0570, 77.6330, 8400, 74, 3.9, 33),
    ("Bengaluru", "City", "North Bengaluru", 13.1020, 77.6250, 7000, 69, 4.0, 27),
    ("Delhi", "City", "Dwarka", 28.5921, 77.0460, 11200, 82, 3.2, 22),
    ("Delhi", "City", "Rohini", 28.7495, 77.0565, 8900, 75, 3.5, 20),
    ("Delhi", "City", "Saket", 28.5245, 77.2066, 16800, 90, 3.0, 18),
    ("Delhi", "City", "Vasant Kunj", 28.5200, 77.1580, 18200, 92, 2.9, 20),
    ("Delhi", "City", "South Delhi", 28.5355, 77.2100, 19400, 93, 2.8, 19),
    ("Delhi", "City", "Central Delhi", 28.6304, 77.2177, 21000, 92, 2.7, 18),
    ("Delhi", "City", "East Delhi", 28.6280, 77.2950, 10400, 76, 3.3, 30),
    ("Delhi", "City", "West Delhi", 28.6510, 77.0900, 9800, 75, 3.4, 24),
    ("Delhi", "City", "North Delhi", 28.7041, 77.2025, 10200, 74, 3.3, 23),
    ("Delhi", "City", "Aerocity", 28.5520, 77.1230, 20500, 89, 3.8, 19),
    ("Delhi", "City", "New Delhi", 28.6139, 77.2090, 21500, 91, 2.6, 18),
    ("Delhi", "NCR", "Gurugram", 28.4595, 77.0266, 14200, 86, 3.6, 27),
    ("Delhi", "NCR", "Noida", 28.5355, 77.3910, 10800, 83, 3.8, 25),
    ("Delhi", "NCR", "Greater Noida", 28.4744, 77.5040, 7200, 75, 4.2, 24),
    ("Delhi", "NCR", "Ghaziabad", 28.6692, 77.4538, 6500, 70, 4.0, 29),
    ("Delhi", "NCR", "Faridabad", 28.4089, 77.3178, 6900, 72, 3.9, 25),
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        if session.scalar(select(func.count(User.id))) == 0:
            seed_demo(session)
        seed_workflow_reference_data(session)


def reset_demo_data(session: Any | None = None) -> None:
    if session is not None:
        session.rollback()
        Base.metadata.drop_all(session.bind)
        Base.metadata.create_all(session.bind)
        seed_demo(session)
        seed_workflow_reference_data(session)
        return
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as new_session:
        seed_demo(new_session)
        seed_workflow_reference_data(new_session)


def seed_workflow_reference_data(session: Any) -> None:
    """Add clearly synthetic starter inventory/evidence rows, including on existing DBs."""
    projects = session.scalars(select(Project).order_by(Project.id)).all()
    if projects and session.scalar(select(func.count(ProjectInventory.id))) == 0:
        rows = []
        for project in projects:
            for number, status in enumerate(["Available", "Reserved", "Sold"], start=1):
                rows.append(ProjectInventory(
                    project_id=project.id, unit_label=f"Sample {number:02d}", availability=status,
                    asking_price=project.asking_price, source_name="Synthetic demo seed",
                    verification_status="Synthetic · unverified", updated_at=datetime.utcnow()))
        session.add_all(rows)
    if projects and session.scalar(select(func.count(ProjectEvidence.id))) == 0:
        evidence_types = ["RERA registration", "Title chain / EC", "Khata / property record",
                          "Property tax", "Technical / occupancy clearance", "Bank approval letter",
                          "Project asking price"]
        session.add_all([
            ProjectEvidence(project_id=project.id, evidence_type=kind, status="Not connected",
                            source_name="No connected provider", verification_status="Unverified")
            for project in projects for kind in evidence_types
        ])
    session.commit()


def seed_demo(session: Any) -> None:
    rng = random.Random(8427)
    locality_rows: list[Locality] = []
    for city, scope, name, lat, lon, rate, score, yield_pct, flood in LOCALITIES:
        locality_rows.append(Locality(city=city, scope=scope, name=name, latitude=lat, longitude=lon,
                                      base_rate=rate, locality_score=score, rental_yield=yield_pct,
                                      flood_score=flood))
    session.add_all(locality_rows)
    session.flush()
    locality_by_name = {item.name: item for item in locality_rows}
    builder_rows = [Builder(name=f"{prefix} {suffix}", city="Bengaluru" if i % 2 == 0 else "Delhi",
                            segment=["Premium", "Mid-market", "Value", "Boutique"][i % 4])
                    for i, (prefix, suffix) in enumerate([
                        ("Aster", "Habitat"), ("Cedar", "Living"), ("Meridian", "Homes"),
                        ("Northstar", "Developers"), ("Evergreen", "Properties"),
                        ("Bluepeak", "Realty"), ("Oakline", "Estates"), ("Riverstone", "Buildcon"),
                        ("Urbanfield", "Spaces"), ("Summit", "Landmarks"), ("Silverleaf", "Housing"),
                        ("Pioneer", "Projects"), ("Crestview", "Realty"), ("Amber", "Developers"),
                        ("Horizon", "Living"), ("Cobalt", "Homes"), ("Greenway", "Estates"),
                        ("Juniper", "Buildcon"), ("Westbridge", "Properties"), ("Highland", "Spaces"),
                        ("Mosaic", "Habitat"), ("Stonehaven", "Realty"), ("Orchid", "Projects"),
                        ("Parkside", "Homes"), ("Arbor", "Landmarks"), ("Vertex", "Living"),
                        ("Goldenfield", "Estates"), ("Hearthstone", "Developers"), ("Suncrest", "Realty"),
                        ("Lakeside", "Homes"), ("Meadow", "Properties"), ("Crownline", "Buildcon"),
                        ("Eastgate", "Spaces"), ("Ironwood", "Habitat"), ("Citrine", "Projects"),
                        ("Redwood", "Living"), ("Kingswell", "Estates"), ("Willow", "Developers"),
                        ("Bridgewater", "Realty"), ("Bluebell", "Homes"),
                    ])]
    session.add_all(builder_rows)
    session.flush()
    projects: list[Project] = []
    configs = [("1 BHK", 700), ("2 BHK", 1120), ("3 BHK", 1580), ("4 BHK", 2200)]
    amenities = ["Clubhouse", "Gym", "EV charging", "Rainwater harvesting", "Co-working lounge", "Play area"]
    for i in range(100):
        loc = locality_rows[i % len(locality_rows)]
        builder = builder_rows[i % len(builder_rows)]
        config, base_area = configs[(i * 7 + i // 3) % len(configs)]
        area = base_area + rng.randint(-90, 180)
        psf = loc.base_rate * rng.uniform(0.88, 1.18)
        asking = int(area * psf)
        tx = int(asking * rng.uniform(0.90, 0.98))
        launch_year = rng.randint(2017, 2025)
        possession = str(rng.choice([2025, 2026, 2027, 2028, 2029]))
        projects.append(Project(
            builder_id=builder.id, locality_id=loc.id,
            name=f"{loc.name} {['Crest', 'Residences', 'Gardens', 'Terraces', 'Heights'][i % 5]} {i + 1:02d}",
            city=loc.city, property_type="Apartment" if i % 8 else "Villa", configuration=config,
            area_sqft=area, asking_price=asking, transaction_price=tx,
            monthly_rent=int(area * loc.base_rate / 12 * loc.rental_yield / 100),
            latitude=loc.latitude + rng.uniform(-0.012, 0.012), longitude=loc.longitude + rng.uniform(-0.012, 0.012),
            tower_count=rng.randint(2, 8), floors=rng.randint(10, 36), launch_year=launch_year,
            possession=possession, status=rng.choice(["Under construction", "Ready to move", "New launch"]),
            amenities_json=json.dumps(rng.sample(amenities, 4)), legal_status="Synthetic demo record"))
    session.add_all(projects)
    session.flush()

    # Thirty-six monthly observations keep asking prices separate from simulated transactions.
    observations: list[MarketObservation] = []
    current = date.today().replace(day=1)
    for loc in locality_rows:
        for ago in range(35, -1, -1):
            year = current.year
            month = current.month - ago
            while month <= 0:
                month += 12
                year -= 1
            month_date = date(year, month, 1)
            trend = 1 + (35 - ago) * rng.uniform(0.0015, 0.0035)
            ask_psf = loc.base_rate * trend * rng.uniform(0.975, 1.025)
            observations.append(MarketObservation(
                locality_id=loc.id, month=month_date,
                asking_psf=round(ask_psf, 2), transaction_psf=round(ask_psf * rng.uniform(0.91, 0.98), 2),
                rent_psf=round(ask_psf * loc.rental_yield / 1200 * rng.uniform(0.96, 1.04), 2),
                available_units=rng.randint(120, 760), absorption_pct=round(rng.uniform(2.1, 8.5), 2),
                enquiries=rng.randint(35, 440)))
    session.add_all(observations)

    sales: list[PropertyTransaction] = []
    for i in range(8000):
        project = projects[i % len(projects)]
        days_ago = rng.randint(0, 1095)
        age_ratio = days_ago / 1095
        area = max(400, int(project.area_sqft * rng.uniform(.91, 1.09)))
        unit_price = project.transaction_price * (1 - age_ratio * .12) * rng.uniform(.94, 1.06)
        sales.append(PropertyTransaction(
            project_id=project.id, locality_id=project.locality_id,
            observed_on=(datetime.utcnow() - timedelta(days=days_ago)).date(),
            transaction_type="New sale" if i % 3 else "Resale", configuration=project.configuration,
            area_sqft=area, price_inr=int(unit_price * area / max(project.area_sqft, 1))))
    rents: list[RentalTransaction] = []
    for i in range(2000):
        project = projects[(i * 7) % len(projects)]
        days_ago = rng.randint(0, 1095)
        area = max(400, int(project.area_sqft * rng.uniform(.92, 1.08)))
        rents.append(RentalTransaction(
            project_id=project.id, locality_id=project.locality_id,
            observed_on=(datetime.utcnow() - timedelta(days=days_ago)).date(),
            configuration=project.configuration, area_sqft=area,
            monthly_rent_inr=int(project.monthly_rent * rng.uniform(.82, 1.22)),
            vacancy_days=rng.randint(0, 55)))
    session.bulk_save_objects(sales)
    session.bulk_save_objects(rents)
    session.flush()

    # 2,000 synthetic buyer records, plus demo login accounts.
    first_names = ["Aarav", "Anaya", "Ishaan", "Myra", "Kabir", "Aditi", "Vivaan", "Diya", "Reyansh", "Meera",
                   "Arjun", "Ira", "Vihaan", "Sara", "Rohan", "Nisha", "Dev", "Tara", "Karan", "Riya"]
    last_names = ["Shah", "Rao", "Mehta", "Kapoor", "Nair", "Iyer", "Bose", "Khanna", "Menon", "Jain"]
    timelines = ["Within 3 months", "Within 6 months", "Within 12 months", "Exploring"]
    buyer_rows: list[User] = []
    for i in range(2000):
        loc = locality_rows[(i * 13 + i // 17) % len(locality_rows)]
        buyer_rows.append(User(
            role="buyer", name=f"{first_names[i % len(first_names)]} {last_names[(i // 5) % len(last_names)]}",
            email=("demo.buyer@propclue.test" if i == 0 else f"buyer-{i + 1:04d}@propclue.test"),
            city=loc.city, locality=loc.name,
            budget_min=rng.choice([7000000, 10000000, 12500000, 15000000, 20000000]),
            budget_max=rng.choice([15000000, 20000000, 25000000, 35000000, 50000000]),
            configuration=rng.choice(["1 BHK", "2 BHK", "3 BHK", "4 BHK"]),
            timeline=rng.choice(timelines), created_at=datetime.utcnow() - timedelta(days=rng.randint(0, 900))))
    session.add_all(buyer_rows)
    session.flush()
    admin_user = User(role="admin", name="PropClue Demo Admin", email="demo.admin@propclue.test",
                      city="Bengaluru", locality="Whitefield")
    builder_user = User(role="builder", name="Aster Demo Builder", email="demo.builder@propclue.test",
                        city="Bengaluru", locality="Whitefield",
                        builder_id=builder_rows[0].id)
    session.add_all([admin_user, builder_user])
    session.flush()

    purposes = ["analytics", "personalization", "builder_marketing"]
    consent_rows: list[ConsentRecord] = []
    for buyer in buyer_rows:
        for purpose in purposes:
            granted = (purpose == "analytics" and rng.random() < 0.88) or (purpose == "personalization" and rng.random() < 0.70) or (purpose == "builder_marketing" and rng.random() < 0.58)
            if buyer.id == buyer_rows[0].id:
                granted = True
            consent_rows.append(ConsentRecord(user_id=buyer.id, purpose=purpose, granted=granted,
                                                recorded_at=datetime.utcnow() - timedelta(days=rng.randint(0, 220))))
    session.add_all(consent_rows)
    session.flush()

    # Exactly 30,000 activity records with an explainable synthetic mix.
    event_counts = [
        ("Enquiry submitted", 500), ("Contact request", 300), ("Project shortlisted", 2500),
        ("Property compared", 1500), ("Report downloaded", 1200), ("Project viewed", 10000),
        ("Property viewed", 5000), ("Locality viewed", 5000), ("Map area searched", 2000),
        ("Repeat project view", 1000), ("Price filter applied", 1000),
    ]
    event_rows: list[Event] = []
    for event_type, count in event_counts:
        for j in range(count):
            buyer = buyer_rows[rng.randrange(len(buyer_rows))]
            project = projects[rng.randrange(len(projects))]
            event_rows.append(Event(
                buyer_id=buyer.id, project_id=project.id, city=project.city, locality=project.locality.name,
                event_type=event_type, event_at=datetime.utcnow() - timedelta(days=rng.randint(0, 180), hours=rng.randint(0, 23)),
                session_id=f"demo-session-{rng.randrange(1, 8001):05d}",
                consent_status="analytics granted" if rng.random() < .88 else "pseudonymous",
                context_json=json.dumps({"budget_band": "₹1–2 Cr" if buyer.budget_max <= 20000000 else "₹2 Cr+",
                                         "configuration": buyer.configuration})))
    session.bulk_save_objects(event_rows)
    session.flush()

    eligible_buyers = [buyer for buyer in buyer_rows if any(
        record.user_id == buyer.id and record.purpose == "builder_marketing" and record.granted
        for record in consent_rows)]
    lead_rows: list[Lead] = []
    projects_by_builder: dict[int, list[Project]] = {}
    for project in projects:
        projects_by_builder.setdefault(project.builder_id, []).append(project)
    for i in range(300):
        buyer = eligible_buyers[i % len(eligible_buyers)]
        # Model the builder's authorized portfolio. The account only sees its own rows.
        builder_id = builder_rows[i % len(builder_rows)].id
        portfolio = projects_by_builder[builder_id]
        project = portfolio[(i // len(builder_rows)) % len(portfolio)]
        user_events = session.scalars(select(Event).where(Event.buyer_id == buyer.id).order_by(Event.event_at.desc()).limit(40)).all()
        score, category, _ = score_from_events(list(user_events))
        lead_rows.append(Lead(builder_id=builder_id, buyer_id=buyer.id, project_id=project.id,
                              status=rng.choice(["New", "Contact pending", "Contacted", "Interested", "Site visit scheduled", "Negotiation"]),
                              intent_score=score, intent_category=category,
                              last_activity=max((event.event_at for event in user_events), default=datetime.utcnow()),
                              assigned_to=f"Sales {i % 8 + 1}"))
    session.add_all(lead_rows)
    session.flush()

    saved_rows = [SavedProperty(user_id=buyer_rows[rng.randrange(len(buyer_rows))].id,
                                project_id=projects[rng.randrange(len(projects))].id,
                                saved_at=datetime.utcnow() - timedelta(days=rng.randint(0, 180))) for _ in range(700)]
    session.add_all(saved_rows)
    session.add_all([
        Campaign(builder_id=builder_rows[0].id, name="Whitefield 3 BHK interest", segment="3 BHK · Whitefield · opted in",
                 audience_size=42, eligible_count=31, simulated_clicks=12, status="Simulated"),
        Campaign(builder_id=builder_rows[0].id, name="North Bengaluru new launch", segment="High intent · last 30 days · opted in",
                 audience_size=28, eligible_count=19, simulated_clicks=7, status="Simulated"),
    ])
    sources = [
        ("Asking price trends", "Market data", "Synthetic generator", "Synthetic", "Bengaluru + Delhi", "Demo-only", "Synthetic demo record", "Critical", "36-month asking-price series"),
        ("Transaction benchmarks", "Market data", "Synthetic generator", "Synthetic", "Bengaluru + Delhi", "Demo-only", "Synthetic demo record", "Critical", "Simulated transaction-price series"),
        ("Land and legal records", "Legal", "Landeed / authorized state source", "Potential provider", "State-specific", "Not procured", "Not connected", "Critical", "Synthetic due-diligence checklist"),
        ("Building footprints", "Geospatial", "Nearmap / licensed imagery", "Potential provider", "City-level", "Not procured", "Not connected", "High", "Synthetic project points"),
        ("Parcel boundaries", "Geospatial", "Authorized cadastral GIS", "Potential provider", "State-specific", "Not procured", "Not connected", "High", "Illustrative locality areas"),
        ("Roads and POIs", "Location", "OpenStreetMap / licensed POI", "Potential provider", "City-level", "Not connected", "Not connected", "High", "Synthetic points of interest"),
        ("Environmental indicators", "Environmental", "Copernicus / licensed provider", "Potential provider", "Regional", "Not procured", "Not connected", "Medium", "Synthetic risk scores"),
        ("Listings and rents", "Listings", "Builder feeds / licensed aggregator", "Potential provider", "Project-level", "Not procured", "Not connected", "High", "Synthetic listings and rent data"),
        ("RERA and approvals", "Regulatory", "Karnataka RERA / Delhi RERA", "Official source to evaluate", "State-specific", "Not connected", "Not connected", "Critical", "Synthetic document status"),
        ("Transit and infrastructure", "Infrastructure", "Official agency feeds", "Potential provider", "City-level", "Not connected", "Not connected", "Medium", "Synthetic demonstration layer"),
    ]
    session.add_all([DataSource(dataset=a, category=b, source_name=c, source_type=d, coverage=e,
                               licensing_status=f, verification_status=g, priority=h, demo_substitute=i)
                     for a, b, c, d, e, f, g, h, i in sources])
    session.add(AuditLog(actor_id=admin_user.id, action="demo_seeded", detail="Deterministic synthetic records loaded"))
    session.commit()

