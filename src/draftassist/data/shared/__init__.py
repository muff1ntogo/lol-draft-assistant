"""Shared crawl data (docs/shared-data.md): the admin publishes counts and fits to one Turso database as compressed blocks,
teammates pull them into their local SQLite. Nothing outside this package talks to Turso (tests/test_shared.py checks).

    schema.py   what is shared (the table registry), SCHEMA_VERSION, the identity guard
    store.py    SharedStore: HttpSharedStore (Hrana over httpx, decision 1) and FakeSharedStore (tests)
    blocks.py   tables -> blocks (one per patch, tier, queue and table; gzip; content hash) and back
    publish.py  the admin: changed blocks only, resumable, tombstones, the quota meter
    pull.py     users: newer blocks into the local databases in one transaction, then the runtime tables derived
    config.py   where the store's address and token come from
    published.py  `draftassist shared-inspect`: what is published, decoded
"""
