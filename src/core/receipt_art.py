"""Local receipt artwork; embedded so the full-receipt image has no external dependency."""
import base64
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "assets" / "receipt-character.png"


@lru_cache(maxsize=1)
def data_uri():
    return "data:image/png;base64," + base64.b64encode(PATH.read_bytes()).decode("ascii")
