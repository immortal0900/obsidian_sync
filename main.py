"""루트 엔트리 — src.main 로 위임."""
from __future__ import annotations

import sys

from src.main import run

if __name__ == "__main__":
    sys.exit(run())
