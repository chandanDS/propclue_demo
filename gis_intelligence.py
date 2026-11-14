"""Isolated third map; illustrative geometry is never source cadastral data."""
from pathlib import Path

import streamlit as st

_ASSETS = Path(__file__).parent / "assets" / "gis_intelligence"
_MAP = st.components.v2.component(
    "propclue_gis_intelligence_v1",
    html=(_ASSETS / "map.html").read_text(encoding="utf-8"),
    css=(_ASSETS / "map.css").read_text(encoding="utf-8"),
    js=(_ASSETS / "map.js").read_text(encoding="utf-8"),
)


def render_gis_intelligence() -> None:
    """Keep navigation and selection entirely inside the new map instance."""
    _MAP(key="gis-intelligence-third-map", height=780, width="stretch")
