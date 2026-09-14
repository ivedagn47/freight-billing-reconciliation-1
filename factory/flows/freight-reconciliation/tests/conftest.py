"""Tests for the freight code layer.

Real data is used only for structure (formats parse, ids, periods, line counts, clause numbers) and
for the confirmed scope rule. Every numeric expectation about pricing uses synthetic carriers,
shipments and invoices, so no test encodes an answer to the actual reconciliation.
"""

import sys
from pathlib import Path

FLOW_DIR = Path(__file__).resolve().parents[1]
REPO = FLOW_DIR.parents[2]
sys.path.insert(0, str(FLOW_DIR))
