"""Every test gets its own schema; existing Payment records are left alone."""

import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app import store


def make_db(test):
    url = os.getenv("PAYMENT_TEST_DB", "postgresql://payment:payment@127.0.0.1:5434/payment")
    schema = "test_payment_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def cleanup():
        # This exact schema was uniquely created above, never public or user supplied.
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    test.addCleanup(cleanup)
    db_url = make_conninfo(url, options=f"-c search_path={schema}")
    store.setup_db(db_url)
    return db_url
