"""Shared test setup.

The sys.path insertion below lived, byte-identical, in 25 test files. It pins
imports to this checkout so the suite exercises the tree: CI installs the
package non-editable, and without the pin a released agentcheck-verify on the
path could shadow the tree and green-light stale code. One place now.

The per-file `_client` helpers stay local on purpose: they differ meaningfully
(https base URL, file-backed db, tuple shape, injected auth provider), and a
single fixture would either lie about those needs or sprout parameters until
it was the same code with more indirection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
