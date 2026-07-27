"""Integration tests that run against a live Amazon Redshift cluster.

Two tiers, two markers, because the two have very different risk profiles:

``redshift``
    Needs a reachable cluster and is **read-only**. Every statement passes a
    client-side guard before it reaches the server, and the session asks the
    server for a READ ONLY transaction on top of that, so this tier is safe to
    point at a warehouse somebody depends on.

``redshift_ddl``
    Needs a **disposable** cluster and mutates it. Refuses to run unless
    ``DP_TEST_RS_DISPOSABLE`` says out loud that the cluster is expendable.

See ``tests/integration/redshift/conftest.py`` for the fixture contract and the
``DP_TEST_RS_*`` environment variables. Everything here is also marked
``integration`` by ``tests/integration/conftest.py``, so ``pytest -m 'not
integration'`` excludes it.
"""
