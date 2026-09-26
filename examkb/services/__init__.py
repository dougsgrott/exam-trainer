"""Services: the logic between the queries and the pages.

`examkb/queries.py` holds read helpers that are one statement each. Anything with
a decision in it -- how a search string becomes an FTS5 expression, how a pool is
sampled, how an attempt is graded -- lives here, so the web layer stays a thin
thing that calls a function and renders what comes back.
"""
