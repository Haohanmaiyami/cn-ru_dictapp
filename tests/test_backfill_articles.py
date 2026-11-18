"""Offline provenance, artifact, and interruption regressions. No DB/network."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_articles as tool
from dictionary_sources import SOURCE_FILENAMES, SourceFormatError, iter_source_articles


def row(values, raw=None, article=None, entry_id=42):
    return dict(values, id=entry_id, raw_dsl=raw, article=article)


def make_sources(directory, first="词\n cí\n [m1]слово[/m]\n", second="", third="", reverse=""):
    for name, text in zip(SOURCE_FILENAMES, (first, second, third, reverse)):
        (directory / name).write_text('#NAME "test version 250920"\n' + text, encoding="utf-16")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        make_sources(self.data)
        self.catalog_path = self.root / "catalog.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        return tool.build_catalog(self.data, self.catalog_path)

    def test_exact_source_matches_legacy_fields_only_and_does_not_mutate_input(self):
        self.build()
        db = tool.read_database(self.catalog_path, "catalog")
        record = db.execute("SELECT * FROM candidates").fetchone()
        old = row(json.loads(record["legacy"]))
        before = dict(old)
        candidate, status = tool.classify(db, old)
        self.assertEqual(status, "ready")
        self.assertEqual(old, before)
        for key in tool.LEGACY:
            changed = dict(old)
            changed[key] = "different" if old[key] is None else old[key] + "!"
            self.assertEqual(tool.classify(db, changed)[1], "no_exact_source")
        db.close()

    def test_identical_duplicate_sources_are_safe_for_multiple_existing_ids(self):
        article = "词\n cí\n [m1]слово[/m]\n"
        make_sources(self.data, article, article)
        self.assertEqual(self.build()["unique_signatures"], 1)
        db = tool.read_database(self.catalog_path, "catalog")
        record = db.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual(record["occurrences"], 2)
        for entry_id in (1, 999):
            self.assertEqual(tool.classify(db, row(json.loads(record["legacy"]), entry_id=entry_id))[1], "ready")
        db.close()

    def test_same_legacy_text_different_markup_is_ambiguous(self):
        make_sources(self.data, "词\n cí\n [m1]слово[/m]\n",
                     "词\n cí\n [m1][i]слово[/i][/m]\n")
        self.assertEqual(self.build()["ambiguous_signatures"], 1)
        db = tool.read_database(self.catalog_path, "catalog")
        record = db.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual(tool.classify(db, row(json.loads(record["legacy"])))[1], "ambiguous_source")
        db.close()

    def test_existing_metadata_is_never_overwritten_without_exact_plan(self):
        self.build()
        db = tool.read_database(self.catalog_path, "catalog")
        record = db.execute("SELECT * FROM candidates").fetchone()
        values = json.loads(record["legacy"])
        self.assertEqual(tool.classify(db, row(values, "user edit"))[1], "populated_conflict")
        self.assertEqual(tool.classify(db, row(values, record["raw_dsl"], record["article"]))[1], "already_current")
        self.assertEqual(tool.classify(db, row(values, record["raw_dsl"]))[1], "populated_conflict")
        db.close()

    def test_reverse_dictionary_is_explicitly_untouched(self):
        make_sources(self.data, reverse="слово\n 词 cí\n")
        counts = self.build()
        self.assertEqual(counts["reverse_articles_untouched"], 1)
        self.assertEqual(counts["unique_signatures"], 1)

    def test_outputs_private_exclusive_and_incomplete_catalog_rejected(self):
        self.build()
        self.assertEqual(stat.S_IMODE(self.catalog_path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            self.build()
        path = self.root / "incomplete.sqlite"
        db = tool.private_database(path)
        tool.put_meta(db, "kind", "catalog")
        tool.put_meta(db, "complete", False)
        db.commit(); db.close()
        with self.assertRaises(tool.SafetyError):
            tool.read_database(path, "catalog")

    def test_changed_sources_leave_unusable_catalog(self):
        before = tool.source_manifest(self.data)
        with patch.object(tool, "source_manifest", side_effect=[before, []]):
            with self.assertRaisesRegex(tool.SafetyError, "changed"):
                self.build()
        with self.assertRaises(tool.SafetyError):
            tool.read_database(self.catalog_path, "catalog")

    def test_source_encoding_and_include_validation(self):
        (self.data / SOURCE_FILENAMES[0]).write_text("词\n word\n", encoding="utf-8")
        with self.assertRaises(SourceFormatError):
            self.build()
        make_sources(self.data)
        path = self.data / SOURCE_FILENAMES[0]
        path.write_text('#INCLUDE "../../outside.dsl"\n词\n word\n', encoding="utf-16")
        with self.assertRaises(SourceFormatError):
            list(iter_source_articles(self.data))

    def test_physical_include_is_not_expanded_twice(self):
        make_sources(self.data, '#INCLUDE "dabkrs_2.dsl"\n词\n cí\n слово\n', "你\n nǐ\n ты\n")
        self.assertEqual(len(list(iter_source_articles(self.data))), 2)

    def test_symlink_inputs_and_outputs_rejected(self):
        self.build()
        link = self.root / "link.sqlite"
        link.symlink_to(self.catalog_path)
        with self.assertRaises(tool.SafetyError):
            tool.read_database(link, "catalog")
        with self.assertRaises(FileExistsError):
            tool.private_database(link)

    def test_journal_is_bound_to_plan_catalog_and_database(self):
        path = self.root / "journal.sqlite"
        target = {"database": "test", "endpoint_sha256": "host-a", "anchor_sha256": "data-a"}
        db = tool.open_journal(path, "plan-a", "catalog-a", target)
        db.close()
        for plan, catalog, identity in (("plan-b", "catalog-a", target),
                                         ("plan-a", "catalog-b", target),
                                         ("plan-a", "catalog-a", dict(target, endpoint_sha256="host-b"))):
            with self.assertRaises(tool.SafetyError):
                tool.open_journal(path, plan, catalog, identity)

    def test_catalog_read_snapshot_prevents_concurrent_input_commit(self):
        self.build()
        reader = tool.read_database(self.catalog_path, "catalog")
        writer = sqlite3.connect(self.catalog_path, timeout=0)
        writer.execute("UPDATE meta SET value='false' WHERE key='complete'")
        with self.assertRaises(sqlite3.OperationalError):
            writer.commit()
        self.assertIs(tool.meta(reader, "complete"), True)
        reader.close()
        writer.commit()
        writer.close()
        with self.assertRaises(tool.SafetyError):
            tool.read_database(self.catalog_path, "catalog")


class RecoveryTests(unittest.TestCase):
    def test_crash_before_postgres_commit_replays_pending_write(self):
        self.assertEqual(tool.transition((None, None), (None, None), ("raw", "{}"), True), "write")

    def test_crash_after_postgres_commit_recognizes_already_applied(self):
        self.assertEqual(tool.transition(("raw", "{}"), (None, None), ("raw", "{}"), True), "already_applied")

    def test_unjournaled_existing_payload_cannot_be_claimed_as_ours(self):
        with self.assertRaises(tool.SafetyError):
            tool.transition(("raw", "{}"), (None, None), ("raw", "{}"), False)

    def test_rollback_pending_uncommitted_and_committed_rows(self):
        self.assertEqual(tool.transition((None, None), (None, None), ("raw", "{}"), True, True), "already_restored")
        self.assertEqual(tool.transition(("raw", "{}"), (None, None), ("raw", "{}"), True, True), "restore")

    def test_apply_and_rollback_refuse_external_metadata_change(self):
        for rollback in (False, True):
            with self.assertRaises(tool.SafetyError):
                tool.transition(("manual", "{}"), (None, None), ("raw", "{}"), True, rollback)

    def test_connections_need_explicit_env_and_do_not_read_dotenv(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(tool.SafetyError):
                tool.connection_options()
        with patch.dict(os.environ, {"TEST_DSN": "postgresql://u:private-password@host/db?host=other"}, clear=True):
            with self.assertRaises(tool.SafetyError):
                tool.connection_options("TEST_DSN")

    def test_connection_identity_excludes_password(self):
        env = {"DB_HOST": "127.0.0.1", "DB_PORT": "15435", "DB_NAME": "test", "DB_USER": "u", "DB_PASSWORD": "one"}
        with patch.dict(os.environ, env, clear=True):
            _, first = tool.connection_options()
            os.environ["DB_PASSWORD"] = "two"
            _, second = tool.connection_options()
        self.assertEqual(first, second)
        self.assertNotIn("one", first)


if __name__ == "__main__":
    unittest.main()
