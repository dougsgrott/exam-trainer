"""The server-rendered web app.

Nothing under this package may import SQLAlchemy or `examkb.models`. Routes call
`examkb/queries.py`, `examkb/status.py` and the services; templates receive plain
dataclasses and dicts. `tests/test_layering.py` walks this tree and fails on the
import, so the rule covers modules nobody has written yet.
"""
