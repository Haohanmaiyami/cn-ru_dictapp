"""Offline API projection regressions: no database, settings, or network needed."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


_network_guard_active = True


def _deny_network(event, args):
    if _network_guard_active and event in {
        "socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind"
    }:
        raise AssertionError("Article presentation tests must stay offline")


sys.addaudithook(_deny_network)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import ValidationError
from sqlalchemy import inspect
from dictapp.dsl_parser import parse_dsl_article
from dictapp.models import Entry
from dictapp.presentation import article_for_entry, present_entry
from dictapp.schemas import DictionaryArticle, EntryOut

_network_guard_active = False


FIXTURES = {row["head"]: row for row in json.loads(
    (ROOT / "tests/fixtures/bkrs_articles.json").read_text())["articles"]}
ENTRY_FIELDS = ("id", "hanzi", "pinyin", "ru", "pos", "examples", "raw_dsl", "article")
LEGACY_KEYS = {"id", "hanzi", "pinyin", "ru", "pos", "examples"}


def legacy_entry(**changes):
    values = dict(id=42, hanzi="你好", pinyin="nǐ hǎo", ru="привет; здравствуйте",
                  pos=None, examples="stored legacy HTML remains untouched")
    values.update(changes)
    return SimpleNamespace(**values)


def row_snapshot(row):
    """Values and SQLAlchemy histories catch mutation even without a DB session."""
    state = inspect(row)
    return (
        copy.deepcopy({name: getattr(row, name) for name in ENTRY_FIELDS}),
        copy.deepcopy({name: (state.attrs[name].history.added,
                              state.attrs[name].history.unchanged,
                              state.attrs[name].history.deleted)
                       for name in ENTRY_FIELDS}),
    )


class OfflineProjectionTestCase(unittest.TestCase):
    def setUp(self):
        global _network_guard_active
        _network_guard_active = True

    def tearDown(self):
        global _network_guard_active
        _network_guard_active = False


class LegacyProjectionTests(OfflineProjectionTestCase):
    def test_row_without_new_attributes_keeps_legacy_contract(self):
        row = legacy_entry()
        before = copy.deepcopy(vars(row))
        result = present_entry(row)
        self.assertIsInstance(result, EntryOut)
        self.assertEqual(result.model_dump(), {
            "id": 42, "hanzi": "你好", "pinyin": "nǐ hǎo",
            "ru": "привет; здравствуйте", "pos": None, "examples": "", "article": None,
        })
        self.assertEqual(vars(row), before)

    def test_old_unstructured_rows_use_established_split(self):
        result = present_entry(legacy_entry(ru="привет 王女士，你好！ Добрый день!"))
        self.assertEqual(result.ru, "привет Добрый день!")
        self.assertEqual(result.examples, "王女士，你好！")
        self.assertIsNone(result.article)

    def test_nullable_legacy_values_remain_safe(self):
        result = present_entry(legacy_entry(ru=None, pinyin=None))
        self.assertEqual(result.ru, "")
        self.assertEqual(result.examples, "")
        self.assertIsInstance(result.pinyin, str)
        self.assertIsNone(result.pos)
        self.assertIsNone(result.article)

    def test_unknown_and_malformed_cached_articles_fall_back(self):
        invalid_values = [
            "not an article", [], {},
            {"version": 2, "senses": [{"definition": "unsupported replacement"}]},
            {"version": 1, "senses": []},
            {"version": 1, "senses": [{"definition": ""}]},
            {"version": 1, "senses": [{"definition": {"bad": "type"}}]},
            {"version": 1, "senses": [{"definition": "text", "examples": "bad type"}]},
        ]
        for cached in invalid_values:
            with self.subTest(cached=cached):
                row = legacy_entry(article=cached, raw_dsl=None)
                before = copy.deepcopy(vars(row))
                self.assertIsNone(article_for_entry(row))
                result = present_entry(row)
                self.assertEqual(result.ru, "привет; здравствуйте")
                self.assertIsNone(result.article)
                self.assertEqual(vars(row), before)

    def test_invalid_cache_and_unsupported_raw_keep_legacy_text(self):
        result = present_entry(legacy_entry(article={"version": 99},
                                            raw_dsl="[m1][unknown]text[/unknown][/m]"))
        self.assertEqual(result.ru, "привет; здравствуйте")
        self.assertIsNone(result.article)


class StructuredProjectionTests(OfflineProjectionTestCase):
    def test_all_six_articles_preserve_ids_legacy_types_and_orm_state(self):
        self.assertEqual(set(FIXTURES), {"你好", "你", "打扰", "什么", "妈逼", "妈屄"})
        for entry_id, (head, fixture) in enumerate(FIXTURES.items(), start=100):
            with self.subTest(head=head):
                raw = fixture["raw_dsl"]
                parsed = parse_dsl_article(raw)
                self.assertIsNotNone(parsed)
                row = Entry(id=entry_id, hanzi=head, pinyin=raw.splitlines()[0],
                            ru="original stored definition", pos=None,
                            examples="original stored examples", raw_dsl=raw, article=parsed)
                before = row_snapshot(row)
                result = present_entry(row)
                payload = json.loads(result.model_dump_json())
                self.assertEqual(payload["id"], entry_id)
                self.assertEqual(payload["hanzi"], head)
                self.assertTrue(LEGACY_KEYS <= payload.keys())
                self.assertIsInstance(payload["id"], int)
                for key in ("hanzi", "pinyin", "ru", "examples"):
                    self.assertIsInstance(payload[key], str)
                self.assertIsNone(payload["pos"])
                self.assertNotIn("raw_dsl", payload)
                self.assertEqual({key: value for key, value in payload["article"].items()
                                  if key != "formatting"}, parsed)
                self.assertEqual(row_snapshot(row), before)
                self.assertTrue(inspect(row).transient)

    def test_valid_raw_recovers_from_invalid_cache_for_every_fixture(self):
        for head, fixture in FIXTURES.items():
            with self.subTest(head=head):
                row = legacy_entry(hanzi=head, raw_dsl=fixture["raw_dsl"],
                                   article={"version": 9, "senses": []})
                before = copy.deepcopy(vars(row))
                result = present_entry(row)
                self.assertEqual(result.article.model_dump(exclude={"formatting"}), parse_dsl_article(fixture["raw_dsl"]))
                self.assertEqual(vars(row), before)

    def test_valid_raw_without_cache_is_projected_without_writing_cache(self):
        row = legacy_entry(raw_dsl=FIXTURES["你好"]["raw_dsl"], article=None)
        result = present_entry(row)
        self.assertEqual(result.ru, "привет, здравствуй (стандартное приветствие)")
        self.assertEqual(result.examples, "王女士，你好！ — Добрый день, госпожа Ван!")
        self.assertIsNone(row.article)

    def test_seven_numbered_meanings_and_paired_examples_survive_legacy_projection(self):
        result = present_entry(legacy_entry(raw_dsl=FIXTURES["什么"]["raw_dsl"]))
        self.assertEqual([line.split(")", 1)[0] for line in result.ru.splitlines()],
                         list("1234567"))
        self.assertEqual(len(result.examples.splitlines()), 12)
        self.assertIn("4) (после глагола) из-за чего?, зачем?", result.ru.splitlines())
        self.assertIn("他们闹什么? — из-за чего они шумят?", result.examples.splitlines())
        self.assertNotIn("4) (после глагола)", result.examples)
        self.assertNotIn("уже 9 часов", result.ru)
        self.assertNotIn("\\[", result.examples)

    def test_reference_keeps_readable_legacy_text_and_structured_target(self):
        result = present_entry(legacy_entry(hanzi="妈逼", raw_dsl=FIXTURES["妈逼"]["raw_dsl"]))
        self.assertEqual(result.ru, "см. 妈屄")
        self.assertEqual(result.examples, "")
        reference = result.article.senses[0].references[0]
        self.assertEqual((reference.target, reference.label), ("妈屄", "妈屄"))

    def test_schema_rejects_unknown_version_and_empty_article(self):
        for value in ({"version": 2, "senses": [{"definition": "word"}]},
                      {"version": 1, "senses": []},
                      {"version": 1, "senses": [{"definition": "   "}]}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DictionaryArticle.model_validate(value)


if __name__ == "__main__":
    unittest.main()
