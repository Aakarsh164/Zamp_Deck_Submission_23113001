import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class DatabasePersistenceTests(unittest.TestCase):
    def test_configured_path_is_respected_and_parent_is_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "persist" / "app.sqlite"
            original = db.DB
            try:
                with patch.dict(os.environ, {"DB_PATH": str(target)}):
                    importlib.reload(db)
                    db.init()
                    connection = db.conn()
                    configured_path = connection.execute(
                        "pragma database_list"
                    ).fetchone()[2]
                    connection.close()

                self.assertEqual(configured_path, str(target))
                self.assertTrue(target.parent.is_dir())
            finally:
                db.DB = original

    def test_initialization_preserves_existing_database_contents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "existing.sqlite"
            original = db.DB
            try:
                db.DB = str(target)
                connection = db.conn()
                connection.execute("create table retained(value text)")
                connection.execute("insert into retained values('keep-me')")
                connection.commit()
                connection.close()

                db.init()

                connection = db.conn()
                retained = connection.execute(
                    "select value from retained"
                ).fetchone()[0]
                connection.close()
                self.assertEqual(retained, "keep-me")
            finally:
                db.DB = original

    def test_all_database_connections_use_the_configured_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "shared.sqlite"
            original = db.DB
            try:
                db.DB = str(target)
                first = db.conn()
                first_path = first.execute("pragma database_list").fetchone()[2]
                first.close()
                second = db.conn()
                second_path = second.execute("pragma database_list").fetchone()[2]
                second.close()

                self.assertEqual(first_path, second_path)
                self.assertEqual(second_path, str(target))
            finally:
                db.DB = original


if __name__ == "__main__":
    unittest.main()
