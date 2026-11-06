"""Shared test setup."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Makes thread interleavings far more likely, so a missing lock actually shows up.
if interval := os.environ.get("OMNILIMIT_TEST_SWITCH_INTERVAL"):
    sys.setswitchinterval(float(interval))
