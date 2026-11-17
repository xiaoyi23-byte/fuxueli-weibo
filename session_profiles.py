"""Per-browser-session profile store for the public, database-free edition."""
from __future__ import annotations

import streamlit as st

from project import demo_profiles


class SessionProfileStore:
    """Implements the profile backup/edit interface without persistent writes."""

    is_cloud = False

    def __init__(self):
        if "public_profiles" not in st.session_state:
            st.session_state.public_profiles = demo_profiles()

    def list(self):
        return st.session_state.public_profiles.copy()

    def save(self, pid, profile):
        st.session_state.public_profiles[pid] = profile

    def save_many(self, profiles, *, overwrite=False):
        existing = st.session_state.public_profiles
        inserted = 0
        for pid, profile in profiles.items():
            if overwrite or pid not in existing:
                existing[pid] = profile
                inserted += 1
        return inserted
