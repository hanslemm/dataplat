"""Integration tests that run against a real PostgreSQL server.

Marked ``integration`` so ``pytest -m 'not integration'`` excludes them; see
``tests/integration/conftest.py`` for the fixture contract and the
DP_TEST_PG_DSN / DP_TEST_PG_REQUIRED environment variables.
"""
