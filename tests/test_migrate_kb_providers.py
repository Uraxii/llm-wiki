"""Regression guard for scripts/migrate-kb-providers.py.

Every case builds its own kb in a tempdir; nothing here reads a checked-in
fixture, and nothing here touches a real kb.
"""

import importlib.util
import io
import contextlib
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmwiki import vectors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate-kb-providers.py"

_spec = importlib.util.spec_from_file_location("migrate_kb_providers", SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrate
_spec.loader.exec_module(migrate)

OLD_CONFIG = """\
# A comment above [models] that must survive.
[models]
summarize = "vendor/small"
embed = "vendor/embed-1"

# A comment above the endpoint table.
[endpoint]
url = "https://example.invalid/v1"

[identifiers.isbn]
pattern = "^\\\\d{13}$"
describe = "13-digit ISBN"
"""


class MigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def kb(self, config: str = OLD_CONFIG) -> Path:
        root = self.tmp / "kb"
        (root / "wiki").mkdir(parents=True)
        (root / "sources").mkdir()
        (root / "config.toml").write_text(config, encoding="utf-8")
        return root

    def migrated(self, config: str = OLD_CONFIG) -> str:
        change = migrate.plan(self.kb(config))
        self.assertIsNotNone(change)
        return change[2]

    def test_refuses_a_directory_that_is_not_a_kb(self) -> None:
        bare = self.tmp / "bare"
        bare.mkdir()
        with self.assertRaises(migrate.Refused) as caught:
            migrate.plan(bare)
        self.assertIn("not a kb", str(caught.exception))

    def test_refuses_a_config_without_the_wiki_directory(self) -> None:
        root = self.kb()
        shutil.rmtree(root / "wiki")
        with self.assertRaises(migrate.Refused) as caught:
            migrate.plan(root)
        self.assertIn("wiki/", str(caught.exception))

    def test_endpoint_table_becomes_a_provider_table(self) -> None:
        config = tomllib.loads(self.migrated())
        self.assertNotIn("endpoint", config)
        self.assertEqual(
            config["providers"]["hosted"]["url"],
            "https://example.invalid/v1",
        )

    def test_every_model_id_gains_the_prefix(self) -> None:
        config = tomllib.loads(self.migrated())
        self.assertEqual(config["models"]["summarize"], "hosted:vendor/small")
        self.assertEqual(config["models"]["embed"], "hosted:vendor/embed-1")

    def test_key_env_names_a_variable_and_holds_no_value(self) -> None:
        table = tomllib.loads(self.migrated())["providers"]["hosted"]
        self.assertEqual(table["key_env"], "LLM_WIKI_API_KEY")

    def test_comments_and_unrelated_tables_survive(self) -> None:
        new = self.migrated()
        self.assertIn("# A comment above [models] that must survive.", new)
        self.assertIn("# A comment above the endpoint table.", new)
        self.assertEqual(
            tomllib.loads(new)["identifiers"]["isbn"]["describe"],
            "13-digit ISBN",
        )

    def test_pdf_part_is_carried_onto_the_provider(self) -> None:
        config = OLD_CONFIG.replace(
            'url = "https://example.invalid/v1"',
            'url = "https://example.invalid/v1"\npdf_part = "image_url"',
        )
        table = tomllib.loads(self.migrated(config))["providers"]["hosted"]
        self.assertEqual(table["pdf_part"], "image_url")

    def test_an_existing_key_file_env_is_not_overwritten(self) -> None:
        config = OLD_CONFIG.replace(
            "[endpoint]\n", '[endpoint]\nkey_file_env = "SOME_PATH_VAR"\n'
        )
        table = tomllib.loads(self.migrated(config))["providers"]["hosted"]
        self.assertEqual(table["key_file_env"], "SOME_PATH_VAR")
        self.assertNotIn("key_env", table)

    def test_a_migrated_config_is_already_current(self) -> None:
        root = self.kb()
        change = migrate.plan(root)
        (root / "config.toml").write_text(change[2], encoding="utf-8")
        self.assertIsNone(migrate.plan(root))

    def test_refuses_a_config_holding_both_shapes(self) -> None:
        config = OLD_CONFIG + '\n[providers.other]\nurl = "http://x.invalid"\n'
        with self.assertRaises(migrate.Refused) as caught:
            migrate.plan(self.kb(config))
        self.assertIn("both", str(caught.exception))

    def test_refuses_a_model_id_that_already_holds_a_colon(self) -> None:
        config = OLD_CONFIG.replace('"vendor/small"', '"family:size"')
        with self.assertRaises(migrate.Refused) as caught:
            migrate.plan(self.kb(config))
        self.assertIn("colon", str(caught.exception))

    def test_refuses_an_endpoint_key_no_provider_table_accepts(self) -> None:
        config = OLD_CONFIG.replace("[endpoint]\n", "[endpoint]\ntimeout = 5\n")
        with self.assertRaises(migrate.Refused) as caught:
            migrate.plan(self.kb(config))
        self.assertIn("timeout", str(caught.exception))


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "kb"
        (self.root / "wiki").mkdir(parents=True)
        (self.root / "sources").mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(OLD_CONFIG, encoding="utf-8")

    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = migrate.main([*argv])
        return code, out.getvalue()

    def test_the_default_run_writes_nothing(self) -> None:
        code, output = self.run_main(str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("would rewrite", output)
        self.assertIn("dry run: nothing written", output)
        self.assertEqual(self.config.read_text(encoding="utf-8"), OLD_CONFIG)

    def test_apply_writes_the_migrated_config(self) -> None:
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("rewrote", output)
        self.assertNotIn("dry run", output)
        self.assertNotIn("endpoint", tomllib.loads(
            self.config.read_text(encoding="utf-8")
        ))

    def test_a_refusal_exits_nonzero(self) -> None:
        bare = self.tmp / "bare"
        bare.mkdir()
        code, output = self.run_main(str(bare))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", output)

    def test_apply_and_dry_run_together_are_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--apply", "--dry-run", str(self.root))
        self.assertEqual(caught.exception.code, 2)


class VectorRenameTest(unittest.TestCase):
    """The embed id gains a prefix, so the database named after it must
    move with the config or every page is re-embedded for nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "kb"
        (self.root / "wiki").mkdir(parents=True)
        (self.root / "sources").mkdir()
        (self.root / "config.toml").write_text(OLD_CONFIG, encoding="utf-8")

    def vectors(self, *names: str) -> Path:
        directory = self.root / "vectors"
        directory.mkdir(exist_ok=True)
        for name in names:
            (directory / name).write_bytes(name.encode())
        return directory

    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = migrate.main([*argv])
        return code, out.getvalue()

    def test_the_database_and_both_siblings_move_together(self) -> None:
        directory = self.vectors(
            "vendor--embed-1.sqlite",
            "vendor--embed-1.sqlite-wal",
            "vendor--embed-1.sqlite-shm",
        )
        code, _ = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()),
            [
                "hosted--vendor--embed-1.sqlite",
                "hosted--vendor--embed-1.sqlite-shm",
                "hosted--vendor--embed-1.sqlite-wal",
            ],
        )

    def test_the_moved_database_keeps_its_bytes(self) -> None:
        directory = self.vectors("vendor--embed-1.sqlite")
        self.run_main("--apply", str(self.root))
        moved = directory / "hosted--vendor--embed-1.sqlite"
        self.assertEqual(moved.read_bytes(), b"vendor--embed-1.sqlite")

    def test_a_dry_run_moves_nothing_and_names_the_rename(self) -> None:
        directory = self.vectors("vendor--embed-1.sqlite")
        code, output = self.run_main(str(self.root))
        self.assertEqual(code, 0)
        self.assertIn(
            "would rename vectors/vendor--embed-1.sqlite -> "
            "hosted--vendor--embed-1.sqlite",
            output,
        )
        self.assertEqual(
            [path.name for path in directory.iterdir()],
            ["vendor--embed-1.sqlite"],
        )

    def test_a_missing_vectors_directory_is_not_an_error(self) -> None:
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("no database to move", output)
        self.assertFalse((self.root / "vectors").exists())

    def test_a_vectors_directory_holding_no_database_is_not_an_error(
        self,
    ) -> None:
        self.vectors("unrelated.txt")
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("no database to move", output)

    def test_both_names_present_is_refused_and_writes_nothing(self) -> None:
        self.vectors(
            "vendor--embed-1.sqlite", "hosted--vendor--embed-1.sqlite"
        )
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("both exist", output)
        self.assertEqual(
            (self.root / "config.toml").read_text(encoding="utf-8"),
            OLD_CONFIG,
        )

    def test_a_second_apply_is_already_current_and_moves_nothing(self) -> None:
        directory = self.vectors("vendor--embed-1.sqlite")
        self.run_main("--apply", str(self.root))
        before = {
            path.name: path.read_bytes() for path in directory.iterdir()
        }
        config = (self.root / "config.toml").read_text(encoding="utf-8")
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("already current", output)
        self.assertEqual(
            {path.name: path.read_bytes() for path in directory.iterdir()},
            before,
        )
        self.assertEqual(
            (self.root / "config.toml").read_text(encoding="utf-8"), config
        )

    def test_a_config_without_an_embed_step_renames_nothing(self) -> None:
        (self.root / "config.toml").write_text(
            OLD_CONFIG.replace('embed = "vendor/embed-1"\n', ""),
            encoding="utf-8",
        )
        self.vectors("vendor--embed-1.sqlite")
        code, output = self.run_main("--apply", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("no database to move", output)

    def test_the_new_name_matches_what_the_cli_will_look_for(self) -> None:
        """Derived from `vectors.db_path`, never from a literal, so a
        change to the slug rules cannot silently split the two."""
        moves = migrate._vector_renames(
            self.root, "vendor/embed-1", "hosted:vendor/embed-1"
        )
        self.assertEqual(moves, [])
        self.vectors("vendor--embed-1.sqlite")
        moves = migrate._vector_renames(
            self.root, "vendor/embed-1", "hosted:vendor/embed-1"
        )
        self.assertEqual(
            moves[0][1].name, f"{vectors.slug('hosted:vendor/embed-1')}.sqlite"
        )


if __name__ == "__main__":
    unittest.main()
