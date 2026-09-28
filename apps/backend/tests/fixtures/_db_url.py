"""Which database the test suite talks to. One definition, imported by both.

A leaf module on purpose: `conftest.py` needs this value before collection (it
refuses to run against anything that is not a throwaway), and `fixtures/db.py`
is registered in `pytest_plugins`. Importing the plugin from conftest to reach
the constant would import it before pytest can rewrite its assertions —
`PytestAssertRewriteWarning: Module already imported so cannot be rewritten`,
which is a hard failure under `filterwarnings = error`. Nothing imports this
file as a plugin, so it can be imported as early as anything likes.
"""

from __future__ import annotations

import os

# A DEDICATED variable, NOT the application's own DATABASE__URL. The suite DROPs
# and recreates the public schema on first use, so reading the app's variable
# meant anyone with it exported — the ordinary state on a machine that also runs
# the dev stack — ran the whole suite against the dev database.
#
# Inside the `tests` service container the test DB is `timescaledb-test:5432`;
# from the host it is published on `localhost:25432`, which is the default.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://luxswirl_test:luxswirl_test@localhost:25432/luxswirl_test",
)
