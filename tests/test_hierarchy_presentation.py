"""V2 data contract and real ASGI routes with in-memory rows, no database/network."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import ValidationError
from dictapp.dsl_parser import parse_dsl_article
from dictapp.models import Entry
from dictapp.presentation import present_entry
from dictapp.schemas import DictionaryArticle


def fixtures():
    return json.loads((ROOT / "tests/fixtures/bkrs_hierarchy_articles.json").read_text())["articles"]


def entry_for(fixture, entry_id):
    raw = fixture["raw_dsl"]
    return Entry(id=entry_id, hanzi=fixture["head"], pinyin=raw.splitlines()[0],
                 ru="сохранённый перевод", examples="сохранённый пример", pos=None,
                 raw_dsl=raw, article=parse_dsl_article(raw))


async def offline_api_payloads():
    """Exercise FastAPI serialization; repository and session are explicit fakes."""
    import os
    import httpx
    from pydantic_settings import BaseSettings
    settings_init = BaseSettings.__init__

    def offline_settings(instance, *args, **kwargs):
        kwargs['_env_file'] = None
        return settings_init(instance, *args, **kwargs)

    # Never read the developer's .env or inherited provider credentials.
    # Engine construction is lazy; all sessions and repository calls below
    # are replaced. The test runner also denies every network connection.
    with patch.dict(os.environ, {
        'DB_HOST': '127.0.0.1', 'DB_PORT': '1', 'DB_NAME': 'offline_test',
        'DB_USER': 'offline_test', 'DB_PASSWORD': 'unused_test_value',
        'AI_BASE_URL': 'http://127.0.0.1:1', 'AI_API_KEY': '',
        'OLLAMA_BASE_URL': 'http://127.0.0.1:1',
    }, clear=True), patch.object(BaseSettings, '__init__', offline_settings):
        import dictapp.main as main
    originals = json.loads((ROOT / "tests/fixtures/bkrs_articles.json").read_text())["articles"]
    rows = [entry_for(fixture, 5000 + index)
            for index, fixture in enumerate(originals + fixtures())]
    marker = object()

    async def session():
        yield marker

    async def search(db, q, limit):
        assert db is marker
        return [row for row in rows if row.hanzi == q][:limit]

    async def get_entry(db, entry_id):
        assert db is marker
        return next((row for row in rows if row.id == entry_id), None)

    old_overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[main.get_session] = session
    try:
        with patch.object(main, "search_entries", side_effect=search), \
             patch.object(main, "get_entry_by_id", side_effect=get_entry), \
             patch.object(type(main.engine), "connect", side_effect=AssertionError("No DB connection allowed")):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18765",
                                         trust_env=False) as client:
                payloads = []
                for row in rows:
                    search_result = await client.get("/api/search", params={"q": row.hanzi})
                    detail = await client.get(f"/api/entry/{row.id}")
                    assert search_result.status_code == detail.status_code == 200
                    assert detail.json() == search_result.json()["results"][0]
                    assert detail.json()["id"] == row.id
                    assert "raw_dsl" not in detail.json()
                    payloads.append(detail.json())
                missing = await client.get("/api/entry/999999")
                assert missing.status_code == 404
                return payloads
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(old_overrides)


class HierarchyProjectionTests(unittest.TestCase):
    def test_uncached_recovery_is_read_only_and_preserves_identity_and_raw(self):
        fixture = next(row for row in fixtures() if row['head'] == '一')
        row = entry_for(fixture, 42)
        row.article = None
        before = copy.deepcopy({key: getattr(row, key) for key in
                                ['id', 'hanzi', 'pinyin', 'ru', 'examples', 'raw_dsl', 'article']})
        result = present_entry(row)
        self.assertEqual(result.article.version, 2)
        self.assertIn('Примечание:', result.article.blocks[-1].text)
        self.assertEqual(result.id, 42)
        self.assertEqual(before, {key: getattr(row, key) for key in before})

    def test_all_structured_fixtures_keep_text_ids_and_storage(self):
        for index, fixture in enumerate(fixtures()):
            with self.subTest(head=fixture["head"]):
                row = entry_for(fixture, 100 + index)
                before = copy.deepcopy({key: getattr(row, key) for key in
                                        ["id", "hanzi", "pinyin", "ru", "examples", "raw_dsl", "article"]})
                result = present_entry(row)
                self.assertEqual(result.id, row.id)
                if fixture["expected_version"] is None:
                    self.assertIsNone(result.article)
                    self.assertEqual(result.ru, "сохранённый перевод")
                else:
                    parsed = row.article
                    self.assertEqual(result.article.model_dump(exclude={"formatting"}), parsed)
                    self.assertEqual(json.loads(result.article.model_dump_json(exclude={"formatting"})), parsed)
                    self.assertNotIn("senses", result.article.model_dump())
                    expected_definitions = [
                        (str(block["number"]) + ") " if block["number"] else "") + block["text"]
                        for block in parsed["blocks"] if block["kind"] == "definition"]
                    self.assertEqual([line.strip() for line in result.ru.splitlines()], expected_definitions)
                    self.assertEqual(result.examples.splitlines(),
                                     [block["text"] for block in parsed["blocks"] if block["kind"] == "example"])
                self.assertEqual(before, {key: getattr(row, key) for key in before})

    def test_invalid_cached_v2_uses_valid_raw_without_writing_cache(self):
        fixture = next(row for row in fixtures() if row["head"] == "散")
        row = entry_for(fixture, 77)
        row.article = {"version": 2, "blocks": []}
        result = present_entry(row)
        self.assertEqual(result.article.version, 2)
        self.assertEqual(len(result.article.blocks), 56)
        self.assertEqual(row.article, {"version": 2, "blocks": []})

    def test_malformed_v2_never_replaces_legacy_content(self):
        block = dict(level=2, kind="definition", number="1", text="значение", references=[],
                     chinese=None, translation=None)
        invalid = [{"version": 3, "blocks": [block]}, {"version": 2, "blocks": []},
                   {"version": 2, "blocks": [block], "senses": [{"definition": "mixed"}]}]
        for change in [{"level": 0}, {"level": 5}, {"level": True}, {"level": "2"},
                       {"kind": "unknown"}, {"number": "I"}, {"number": "１"}, {"text": " "},
                       {"chinese": "你好", "translation": "привет"},
                       {"references": [{"target": "", "label": "слово"}]},
                       {"references": [{"target": "你", "label": " "}]},
                       {"kind": "example"},
                       {"kind": "example", "number": None, "chinese": "你好"},
                       {"kind": "example", "number": None, "chinese": "你好", "translation": " "}]:
            invalid.append({"version": 2, "blocks": [dict(block, **change)]})
        for cached in invalid:
            with self.subTest(cached=cached):
                with self.assertRaises(ValidationError):
                    DictionaryArticle.model_validate(cached)
                row = Entry(id=41, hanzi="词", pinyin="cí", ru="прежний перевод", article=cached)
                result = present_entry(row)
                self.assertIsNone(result.article)
                self.assertEqual(result.ru, "прежний перевод")


class HierarchyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_search_detail_serialization_and_old_contract(self):
        payloads = await offline_api_payloads()
        self.assertEqual(len(payloads), 18)
        self.assertEqual(sum(row["article"] is not None and row["article"]["version"] == 2
                             for row in payloads), 12)
        for row in payloads:
            self.assertTrue({"id", "hanzi", "pinyin", "ru", "pos", "examples"} <= row.keys())
            self.assertIsInstance(row["ru"], str)
            self.assertIsInstance(row["examples"], str)


if __name__ == "__main__":
    unittest.main()
