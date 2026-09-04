"""
conftest.py
-----------
Ensures the project root is on sys.path so `from core.retry_engine import
RetryEngine` etc. resolve correctly, mirroring the same pattern already
used in core/simulator.py.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))