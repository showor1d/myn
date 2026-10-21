"""ECB daily reference rates. Public data only; never send spending information."""
import json
import os
import ssl
import subprocess
import time
import uuid
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

SOURCE = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
MAX_BYTES = 32_000_000


def _download():
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "MYN-expenses/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read(MAX_BYTES + 1)
    except urllib.error.URLError as error:
        # Schannel can build the server's intermediate certificate chain using Windows.
        # Keep certificate verification enabled; never use an insecure TLS context.
        curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if os.name != "nt" or not isinstance(error.reason, ssl.SSLCertVerificationError) or not curl.is_file():
            raise
        result = subprocess.run(
            [str(curl), "--fail", "--silent", "--show-error", "--max-time", "20",
             "--max-filesize", str(MAX_BYTES), "--proto", "=https", "--proto-redir", "=https", SOURCE],
            capture_output=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError('Official exchange rates are unavailable. Try again later') from error
        return result.stdout


def cached_quotes(config):
    """Read-only rates for renderers; never wait for network on the UI thread."""
    try:
        saved = json.loads((Path(config) / "expense-rates.json").read_text(encoding="utf-8"))
        return saved.get("rows", {}) if saved.get("source") == SOURCE else {}
    except (OSError, ValueError, AttributeError):
        return {}


def select_quote(rows, day):
    target = date.fromisoformat(day)
    eligible = sorted(d for d in rows if d <= day)
    if not eligible or (target - date.fromisoformat(eligible[-1])).days > 7:
        raise ValueError('No official exchange rate near the expense date was available; nothing was recorded')
    stamp = eligible[-1]
    for code in ("USD", "JPY", "CNY"):
        number = Decimal(rows[stamp][code])
        if not number.is_finite() or number <= 0:
            raise ValueError('Invalid exchange rate')
    return dict(date=stamp, source=SOURCE, rates=rows[stamp])


def quote(config, day):
    cache = Path(config) / "expense-rates.json"
    saved = {}
    if cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if saved.get("source") == SOURCE and time.time() - saved.get("fetched_at", 0) < 3600:
        rows = saved.get("rows", {})
    else:
        xml = _download()
        if len(xml) > MAX_BYTES:
            raise ValueError('Exchange rate file is too large')
        rows = {}
        for node in ET.fromstring(xml).iter():
            stamp = node.get("time")
            if stamp:
                rates = {c.get("currency"): c.get("rate") for c in node if c.get("currency")}
                if all(c in rates for c in ("USD", "JPY", "CNY")):
                    rows[stamp] = {c: rates[c] for c in ("USD", "JPY", "CNY")}
        if not rows:
            raise ValueError('Invalid official exchange rate data')
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_name(cache.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(dict(source=SOURCE, fetched_at=time.time(), rows=rows)), encoding="utf-8")
        os.replace(str(temp), str(cache))
    return select_quote(rows, day)
