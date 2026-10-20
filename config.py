"""Gizli anahtar okuyucu: ortam degiskeni (.env / GitHub Actions) -> st.secrets sirasi."""
import os
from dotenv import load_dotenv

load_dotenv()


def get_secret(key: str, default=None):
    val = os.environ.get(key)
    if val:
        return val
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default


def get_int(key: str, default: int) -> int:
    try:
        return int(get_secret(key, default))
    except (TypeError, ValueError):
        return default
