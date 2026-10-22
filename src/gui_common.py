"""Imports and small helpers shared by MYN's window modules.

Importing this first sets MYN's data paths and the core import path, exactly as
myn_gui.py did before 0.46.0."""

import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, simpledialog, ttk

from data_location import config_for_db, database_dir, root_for_db, prepare_data, user_data_dir
from visual_core import HolographicCore
import theme
import web_frontend

# Direct source launch resolves core imports without a packaged executable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
from runtime_paths import activate
activate()
# Core module defaults are fixed on import, so set the durable path first.
os.environ["MYN_CONFIG_DIR"] = str(user_data_dir())
os.environ["MYN_DATA_DIR"] = str(database_dir())
from chat import ChatSession  # noqa: E402
import credentials  # noqa: E402
import backup  # noqa: E402
import memory_processor  # noqa: E402
import claude_client  # noqa: E402
import message_log  # noqa: E402
import conversation_log  # noqa: E402
import memory_query  # noqa: E402
import personal_profile  # noqa: E402
import local_tools  # noqa: E402
import web_tools  # noqa: E402
import local_actions  # noqa: E402
import agent_client  # noqa: E402
import self_update  # noqa: E402
import usage_stats  # noqa: E402
import vision  # noqa: E402
import agenda  # noqa: E402
import timekeeper  # noqa: E402
import sheets  # noqa: E402
import presence  # noqa: E402
import silence  # noqa: E402
import storage  # noqa: E402
import scene_memory  # noqa: E402
import tts  # noqa: E402
import stt  # noqa: E402
import conversation_mode  # noqa: E402
import preferences_window  # noqa: E402
from tts_player import MP3Player  # noqa: E402


def greeting_for_hour(hour):
    """Choose the one-line greeting from the user's local clock."""
    if 5 <= hour < 11:
        return 'Good morning'
    if 11 <= hour < 18:
        return 'Good afternoon'
    return 'Good evening'


# Display and speak the same English greeting.
GREETINGS_SPOKEN = {'Good morning': "Good morning", 'Good afternoon': "Good afternoon", 'Good evening': "Good evening"}


def preferred_font_family(root):
    """Pick a legible UI font with multilingual fallback support."""
    available = set(tkfont.families(root))
    for family in ("Segoe UI", "Microsoft YaHei UI", "Noto Sans SC", "PingFang SC"):
        if family in available:
            return family
    return "TkDefaultFont"


def _first_paragraph(text, limit):
    """The opening of a long result, for reading aloud."""
    first = (text or "").strip().split("\n\n")[0].strip()
    return first if len(first) <= limit else first[:limit].rstrip() + "…"


def _source_label(item):
    """A web source as "title（site）" for under an answer."""
    from urllib.parse import urlparse
    site = urlparse(item.get("url", "")).netloc.lower()
    site = site[4:] if site.startswith("www.") else site
    title = " ".join((item.get("title") or "").split())
    if len(title) > 32:
        title = title[:31] + "…"
    return "%s（%s）" % (title, site) if title and site else (title or site or item.get("url", ""))


def tokyo_display(value):
    try:
        return datetime.fromisoformat(value).astimezone(memory_query.TOKYO).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "")
