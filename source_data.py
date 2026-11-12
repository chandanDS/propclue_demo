"""Read-only access to PropClue's local PostgreSQL and MongoDB datasets."""
from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st
from pymongo import MongoClient
from sqlalchemy import create_engine, text


def _secret_or_env(name: str, default: str) -> str:
    try:
        value = st.secrets.get(name)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.environ.get(name, default).strip()


def _pg_url() -> str:
    url = _secret_or_env(
        "PROPCLUE_SOURCE_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5433/propcluev1",
    )
    # This read-only UI uses psycopg 3. Accept provider URLs plus the backend's
    # asyncpg URL without requiring users to rewrite their connection scheme.
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


@st.cache_resource
def _pg_engine(url: str):
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": "-c default_transaction_read_only=on",
            "connect_timeout": 5,
        },
    )


@st.cache_resource
def _mongo_client(uri: str):
    return MongoClient(uri, serverSelectionTimeoutMS=3000, readPreference="secondaryPreferred")


@st.cache_data(ttl=120)
def _load_catalog_rows(schema_version: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch published localities and active listings without writing to either source."""
    del schema_version  # Included in the cache key to invalidate older row shapes.
    engine = _pg_engine(_pg_url())
    with engine.connect() as connection:
        localities = pd.read_sql(text("""
            SELECT l.id, l.name, l.slug, c.slug AS city_slug, c.name AS city,
                   l.area_type::text AS area_type, l.centroid_lat AS latitude,
                   l.centroid_lng AS longitude, l.composite_score,
                   l.connectivity_score, l.lifestyle_score, l.realestate_score,
                   l.future_score, l.economics_score, l.social_score,
                   l.mongodb_id, l.data_last_updated, l.data_tier::text AS data_tier
            FROM public.localities l
            JOIN public.cities c ON c.id = l.city_id
            WHERE l.is_published = true
            ORDER BY c.name, l.name
        """), connection)
        listings = pd.read_sql(text("""
            SELECT id, slug, source, builder_name, project_name, project_type,
                   project_url, rera_number, price_min, price_max, units,
                   construction_status, possession_date, address, locality_slug,
                   city_slug, brochure_url, floor_plan_url, data_confidence,
                   listing_status, tier, created_at, updated_at, is_top_builder
            FROM public.listings
            WHERE listing_status = 'active'
            ORDER BY city_slug, locality_slug, project_name
        """), connection)
    return localities, listings


def _with_locality_matches(localities: pd.DataFrame, listings: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add an exact, city-scoped locality join even when an older cache is returned."""
    if "locality_match_slug" in listings.columns:
        return localities, listings
    locality_suffixes: dict[tuple[str, str], set[str]] = {}
    for row in localities.itertuples(index=False):
        city_slug = str(row.city_slug) if pd.notna(row.city_slug) else ""
        full_slug = str(row.slug) if pd.notna(row.slug) else ""
        suffix = full_slug.removeprefix(f"{city_slug}-") if city_slug else full_slug
        locality_suffixes.setdefault((city_slug, suffix), set()).add(full_slug)
        locality_suffixes.setdefault((city_slug, full_slug), set()).add(full_slug)

    def normalized(value: Any) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", str(value).casefold())

    normalized_names: dict[tuple[str, str], set[str]] = {}
    for row in localities.itertuples(index=False):
        city_slug = str(row.city_slug) if pd.notna(row.city_slug) else ""
        full_slug = str(row.slug) if pd.notna(row.slug) else ""
        suffix = full_slug.removeprefix(f"{city_slug}-") if city_slug else full_slug
        for label in (suffix, full_slug, getattr(row, "name", "")):
            key = normalized(label)
            if key:
                normalized_names.setdefault((city_slug, key), set()).add(full_slug)

    def resolve_locality(row: Any) -> str | None:
        city_slug = str(row.city_slug) if pd.notna(row.city_slug) else ""
        locality_slug = str(row.locality_slug) if pd.notna(row.locality_slug) else ""
        matches = locality_suffixes.get((city_slug, locality_slug), set())
        if len(matches) != 1:
            matches = normalized_names.get((city_slug, normalized(locality_slug)), set())
        return next(iter(matches)) if len(matches) == 1 else None

    listings = listings.copy()
    listings["locality_match_slug"] = listings.apply(resolve_locality, axis=1)
    return localities, listings


def load_catalog() -> tuple[pd.DataFrame, pd.DataFrame]:
    # Bump this key if the cached row shape changes again. The wrapper also
    # repairs a legacy cache that predates `locality_match_slug`.
    localities, listings = _load_catalog_rows("catalog-v4")
    return _with_locality_matches(localities, listings)


@st.cache_data(ttl=120)
def load_builder_directory(city_slug: str) -> pd.DataFrame:
    """Read builder directory context for one market; no profile data is written."""
    engine = _pg_engine(_pg_url())
    with engine.connect() as connection:
        try:
            return pd.read_sql(text("""
                SELECT rank, builder_name, founded_year, segment, notable_projects,
                       presence_note, website, data_confidence, city_slug
                FROM public.top_builders
                WHERE city_slug = :city_slug
                ORDER BY rank, builder_name
            """), connection, params={"city_slug": city_slug})
        except Exception:
            # Some connected PropClue deployments do not yet publish this optional table.
            return pd.DataFrame(columns=["rank", "builder_name", "founded_year", "segment", "notable_projects", "presence_note", "website", "data_confidence", "city_slug"])


@st.cache_data(ttl=120)
def load_area_dna(slug: str, mongodb_id: str = "") -> dict[str, Any] | None:
    uri = _secret_or_env("PROPCLUE_SOURCE_MONGODB_URI", "mongodb://localhost:27017/propcluev1")
    client = _mongo_client(uri)
    client.admin.command("ping")
    try:
        source_db = client.get_default_database()
    except Exception:
        source_db = client["propcluev1"]
    collection = source_db["area_dna"]
    query: dict[str, Any] = {"meta.slug": slug}
    if mongodb_id:
        from bson import ObjectId
        try:
            query = {"_id": ObjectId(mongodb_id)}
        except Exception:
            pass
    document = collection.find_one(query, {"_id": 0})
    return document


def clear_source_cache() -> None:
    _load_catalog_rows.clear()
    load_area_dna.clear()
    load_builder_directory.clear()
