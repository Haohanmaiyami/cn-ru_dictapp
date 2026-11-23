"""Real BRuKS regressions: full articles, stable IDs, conservative provenance."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from dictapp.presentation import present_entry
from dictapp.reverse_article import parse_reverse_article, russian_headword
from dictapp.schemas import DictionaryArticle
from backfill_legacy import project_reverse
import backfill_articles as backfill
from dictionary_sources import SOURCE_FILENAMES

FIXTURES = {row["head"]: row["raw_dsl"] for row in json.loads(
    (ROOT / "tests/fixtures/bruks_articles.json").read_text())["articles"]}


class ReverseArticleTests(unittest.TestCase):
    def test_real_russian_articles_keep_all_translations_examples_and_source(self):
        expected = {"привет": ["敬礼", "问候", "你好", "передайте ему привет", "вам привет"],
                    "плохо": ["坏", "不好", "不利", "缺乏", "不佳", "不及格", "плохо кончить"]}
        for head, required in expected.items():
            with self.subTest(head=head):
                raw = FIXTURES[head]
                stored = parse_reverse_article(head, raw)
                legacy = project_reverse(head, raw)
                row = SimpleNamespace(id=123, **legacy, raw_dsl=raw, article=stored)
                before = copy.deepcopy(vars(row))
                result = present_entry(row)
                self.assertEqual(result.headword, head)
                self.assertEqual(result.headword_language, "ru")
                self.assertEqual(result.id, 123)
                self.assertEqual(result.hanzi, legacy["hanzi"])
                self.assertEqual(result.ru, legacy["ru"])
                body = "\n".join(result.article.row_texts())
                for text in required:
                    self.assertIn(text, body)
                self.assertEqual(vars(row), before)
                self.assertEqual(result.article.source.dictionary, "bruks")

    def test_reference_only_source_has_no_invented_translation(self):
        parsed = parse_reverse_article("приветик", FIXTURES["приветик"])
        article = DictionaryArticle.model_validate(parsed)
        self.assertIn("привет", "\n".join(article.row_texts()))
        self.assertNotIn("你好", "\n".join(article.row_texts()))
        # No old DB row exists: the previous importer discarded these articles.
        self.assertIsNone(project_reverse("приветик", FIXTURES["приветик"]))

    def test_style_and_source_metadata_coexist(self):
        raw = FIXTURES["плохо"]
        row = SimpleNamespace(id=1, **project_reverse("плохо", raw), raw_dsl=raw,
                              article=parse_reverse_article("плохо", raw))
        article = present_entry(row).article
        self.assertIsNotNone(article.formatting)
        self.assertEqual(article.row_texts(), ["".join(run.text for run in runs)
                                              for runs in article.formatting.rows])

    def test_short_russian_gloss_is_not_mislabelled_as_original_headword(self):
        row = SimpleNamespace(id=7, hanzi="你好", pinyin="nǐ hǎo", ru="привет",
                              pos=None, examples="你好 — привет", raw_dsl=None, article=None)
        self.assertIsNone(russian_headword(row))
        self.assertNotIn("headword", present_entry(row).model_dump())
        row.article = {"version": 1, "senses": [{"definition": "привет"}],
                       "source": {"dictionary": "bruks", "language": "ru", "headword": "другое"}}
        self.assertIsNone(russian_headword(row))
        self.assertNotIn("headword", present_entry(row).model_dump())

    def test_reverse_catalog_matches_exact_old_fields_and_keeps_default_forward_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            for name in SOURCE_FILENAMES:
                body = ""
                if name == "dabkrs_1.dsl":
                    body = "你\n nǐ\n [m1]ты[/m]\n"
                if name == "dabruks.dsl":
                    body = "\n".join(head + "\n " + raw for head, raw in FIXTURES.items()) + "\n"
                (sources / name).write_text('#NAME "test"\n' + body, encoding="utf-16")
            counts = backfill.build_catalog(sources, root / "reverse.sqlite", reverse_only=True)
            self.assertEqual(counts["bruks_candidates"], 4)
            self.assertEqual(counts["unmappable"], 1)
            catalog = backfill.read_database(root / "reverse.sqlite", "catalog")
            old = dict(project_reverse("привет", FIXTURES["привет"]), id=44, raw_dsl=None, article=None)
            candidate, status = backfill.classify(catalog, old)
            self.assertEqual(status, "ready")
            self.assertEqual(json.loads(candidate["article"])["source"]["headword"], "привет")
            changed = dict(old, examples="user edit")
            self.assertEqual(backfill.classify(catalog, changed)[1], "no_exact_source")
            changed = dict(old, raw_dsl="manual metadata")
            self.assertEqual(backfill.classify(catalog, changed)[1], "populated_conflict")
            catalog.close()
            forward = backfill.build_catalog(sources, root / "forward.sqlite")
            self.assertEqual(forward["bkrs_candidates"], 1)
            self.assertEqual(forward["reverse_articles_untouched"], 5)


if __name__ == "__main__":
    unittest.main()
