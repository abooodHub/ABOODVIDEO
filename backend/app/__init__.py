"""ABOODVIDEO Platform Backend Package."""
__version__ = "1.1.0"

# Source runs from backend/ and container runs from /app both resolve the same
# shared UI package. Containers already place it on their normal module path.
from pathlib import Path
import sys

_source_root = Path(__file__).resolve().parents[2]
if (_source_root / 'shared' / 'i18n').is_dir() and str(_source_root) not in sys.path:
    sys.path.insert(0, str(_source_root))
