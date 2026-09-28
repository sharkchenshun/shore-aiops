from __future__ import annotations

import ast
import pathlib
import unittest


def _load_fns():
    src = pathlib.Path(__file__).with_name("config.py").read_text()
    tree = ast.parse(src)
    keep = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_pg_url_parts",
            "coerce_sync_database_url",
            "coerce_async_database_url",
        }
    ]
    ns: dict = {}
    exec(compile(ast.Module(keep, type_ignores=[]), "config.py", "exec"), ns)
    return ns["coerce_sync_database_url"], ns["coerce_async_database_url"]


_FNS = _load_fns()
_SYNC = _FNS[0]
_ASYNC = _FNS[1]


class DbUrlCoerceTests(unittest.TestCase):
    def test_sync_rewrites_default_and_wrong_drivers(self):
        self.assertEqual(
            _SYNC("postgresql://u:p@h:5432/db"),
            "postgresql+psycopg2://u:p@h:5432/db",
        )
        self.assertEqual(
            _SYNC("postgres://u:p@h/db?sslmode=require"),
            "postgresql+psycopg2://u:p@h/db?sslmode=require",
        )
        self.assertEqual(
            _SYNC("postgresql+psycopg://u:p@h/db"),
            "postgresql+psycopg2://u:p@h/db",
        )
        self.assertEqual(
            _SYNC("postgresql+asyncpg://u:p@h/db"),
            "postgresql+psycopg2://u:p@h/db",
        )

    def test_sync_keeps_psycopg2(self):
        url = "postgresql+psycopg2://u:p@h/db"
        self.assertEqual(_SYNC(url), url)

    def test_async_rewrites_default_and_sync_drivers(self):
        self.assertEqual(
            _ASYNC("postgresql://u:p@h:5432/db"),
            "postgresql+asyncpg://u:p@h:5432/db",
        )
        self.assertEqual(
            _ASYNC("postgresql+psycopg2://u:p@h/db"),
            "postgresql+asyncpg://u:p@h/db",
        )
        self.assertEqual(
            _ASYNC("postgresql+psycopg://u:p@h/db"),
            "postgresql+asyncpg://u:p@h/db",
        )

    def test_async_keeps_asyncpg(self):
        url = "postgresql+asyncpg://u:p@h/db"
        self.assertEqual(_ASYNC(url), url)


if __name__ == "__main__":
    unittest.main()
