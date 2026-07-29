"""UI utilities for sandbox violations.

Python port of src/utils/sandbox/sandbox-ui-utils.ts.
"""

from __future__ import annotations

import re


def remove_sandbox_violation_tags(text: str) -> str:
    return re.sub(r"<sandbox_violations>[\s\S]*?</sandbox_violations>", "", text)
