"""Affluense V2 — the concurrent execution engine.

A sibling of `affluense`, never a subpackage of it. The rule that keeps V1
stable is one-directional and absolute:

    V2 imports V1.  V1 never imports V2.

`test_v2_isolation.py` enforces it mechanically, alongside a hash manifest of
every V1 module. If a V1 file changes, the suite fails and names it.

What V2 changes is *scheduling*, not *authority*. Every number in the delivered
report is still produced by V1's deterministic layer: `validate.py` gates it,
`risk_score.py` scores it, `output.py` assembles it. No agent invents a verdict.
"""

from __future__ import annotations

__all__ = ["ENGINE", "VERSION"]

ENGINE = "v2"
VERSION = "2.0.0"
