"""Inline styling regressions: preserve text, occurrence order and API semantics."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dictapp.dsl_parser import parse_dsl_article
from dictapp.inline_styles import _source_formatting, enrich_article
from dictapp.presentation import present_entry
from dictapp.schemas import DictionaryArticle


def article(raw):
    parsed = parse_dsl_article(raw)
    if parsed is None:
        raise AssertionError("Test source must have a supported semantic article")
    return enrich_article(DictionaryArticle.model_validate(parsed), raw)


def projected(raw, cached=None):
    return present_entry(SimpleNamespace(id=81, hanzi="词", pinyin="cí", ru="stored legacy",
                                        pos=None, examples="legacy examples", raw_dsl=raw,
                                        article=cached))


def styled_characters(row):
    return [(char, run.italic, run.bold, run.underline, run.color)
            for run in row for char in run.text]


class InlineStylesTests(unittest.TestCase):
    def test_recovered_single_braces_and_unmarked_prose_retain_source_style(self):
        result = article('_\n[i]вступление[/i][m2]Avena {拉} [b]test[/b][/m]Примечание')
        self.assertEqual(result.row_texts(), ['вступление', 'Avena {拉} test', 'Примечание'])
        self.assertTrue(result.formatting.rows[0][0].italic)
        self.assertTrue(result.formatting.rows[1][-1].bold)
        self.assertEqual(result.formatting.rows[1][-1].text, 'test')

    def test_paired_styles_cross_paragraphs_without_bleeding_after_close(self):
        result = article('xiāngjūn\n[m1]до [i]одно из соединений[/m]'
                         '[m2]продолжение[/i] обычный[/m]')
        self.assertEqual(result.row_texts(), ['до одно из соединений', 'продолжение обычный'])
        self.assertEqual([(r.text, r.italic) for r in result.formatting.rows[0]],
                         [('до ', False), ('одно из соединений', True)])
        self.assertEqual([(r.text, r.italic) for r in result.formatting.rows[1]],
                         [('продолжение', True), (' обычный', False)])

    def test_root_cross_references_remain_clickable_with_label_styles(self):
        raw = 'bǔ\n[p]см.[/p] [ref]卟啉[/ref], [ref]卟吩[/ref][m1]-----[/m][p]вм.[/p] [ref]噗[/ref]'
        result = article(raw)
        self.assertEqual(result.row_texts(), ['см. 卟啉, 卟吩', '-----', 'вм. 噗'])
        self.assertEqual([r.target for b in result.blocks for r in b.references], ['卟啉', '卟吩', '噗'])
        self.assertEqual((result.formatting.rows[0][0].text, result.formatting.rows[0][0].color,
                          result.formatting.rows[0][0].italic), ('см.', 'green', True))

    def test_uncertain_annotated_header_remains_visible_with_own_style(self):
        result = article('sàn, [i]помета[/i]\n[m2]значение[/m]')
        self.assertEqual(result.row_texts(), ['sàn, помета', 'значение'])
        self.assertEqual((result.formatting.rows[0][-1].text, result.formatting.rows[0][-1].italic),
                         ('помета', True))

    def test_recovered_raw_cannot_restyle_a_different_cached_link(self):
        raw = '_\n[p]см.[/p] [ref target="甲"]слово[/ref]'
        cached = parse_dsl_article(raw)
        cached['blocks'][0]['references'][0]['target'] = '乙'
        result = projected(raw, cached)
        self.assertIsNone(result.article.formatting)
        self.assertEqual(result.article.blocks[0].references[0].target, '乙')

    def test_real_reported_words_preserve_every_semantic_field_and_row_text(self):
        fixtures = json.loads((ROOT / "tests/fixtures/bkrs_articles.json").read_text())["articles"]
        for fixture in fixtures:
            with self.subTest(head=fixture["head"]):
                raw = fixture["raw_dsl"]
                semantic = parse_dsl_article(raw)
                result = article(raw)
                self.assertEqual(result.model_dump(exclude={"formatting"}), semantic)
                self.assertEqual(projected(raw, semantic).model_dump(exclude={"article"}),
                                 projected(None, semantic).model_dump(exclude={"article"}))
                if result.formatting:
                    self.assertEqual(["".join(run.text for run in row) for row in result.formatting.rows],
                                     result.row_texts())
                if fixture["head"] == "打扰":
                    label = result.formatting.rows[1][0]
                    self.assertEqual((label.text, label.italic, label.color), ("вежл.", True, "green"))
                if fixture["head"] == "什么":
                    self.assertTrue(any(run.italic and "вопросительное" in run.text
                                        for run in result.formatting.rows[0]))
                if fixture["head"] == "妈逼":
                    label = result.formatting.rows[0][0]
                    self.assertEqual((label.text, label.italic, label.color), ("см.", True, "green"))
                    self.assertEqual(result.senses[0].references[0].target, "妈屄")

    def test_all_supported_complex_fixtures_keep_full_rows_and_raw_source(self):
        fixtures = json.loads((ROOT / "tests/fixtures/bkrs_hierarchy_articles.json").read_text())["articles"]
        for fixture in fixtures:
            if fixture["expected_version"] is None:
                continue
            with self.subTest(head=fixture["head"]):
                raw = fixture["raw_dsl"]
                before = str(raw)
                result = article(raw)
                self.assertIsNotNone(result.formatting)
                self.assertEqual(result.model_dump(exclude={"formatting"}), parse_dsl_article(raw))
                self.assertEqual(result.row_texts(), ["".join(run.text for run in row)
                                                    for row in result.formatting.rows])
                self.assertEqual(raw, before)

    def test_nested_styles_and_repeated_words_are_positional(self):
        result = article("[m1][i]тот же [b][u]тот[/u][/b][/i] тот[/m]")
        chars = styled_characters(result.formatting.rows[0])
        self.assertEqual("".join(item[0] for item in chars), "тот же тот тот")
        self.assertTrue(all(item[1] for item in chars[:10]))
        self.assertTrue(all(item[2] and item[3] for item in chars[7:10]))
        self.assertTrue(all(not any(item[1:4]) for item in chars[10:]))

    def test_crossed_styles_keep_scope_in_v2(self):
        result = article("[m1][c red][i]красный курсив[/c] только курсив[/i] обычный[/m]")
        self.assertEqual(result.version, 2)
        runs = result.formatting.rows[0]
        self.assertEqual([(run.text, run.italic, run.color) for run in runs],
                         [("красный курсив", True, "red"),
                          (" только курсив", True, None), (" обычный", False, None)])

    def test_whitespace_bom_and_escaped_brackets_keep_styles_and_visible_text(self):
        result = article("[m1]\ufeff  [i]раз\u00a0\n  два[/i]  \\[три\\] \\{четыре\\}[/m]")
        self.assertEqual(result.row_texts(), ["раз два [три] {четыре}"])
        self.assertEqual(result.formatting.rows[0][0].text, "раз два")
        self.assertTrue(result.formatting.rows[0][0].italic)
        self.assertNotIn("\\", result.row_texts()[0])

    def test_emoji_combining_marks_and_supplementary_hanzi_are_not_split_or_lost(self):
        result = article("[m1][u]𠀀 e\u0301 👨‍👩‍👧[/u] тест[/m]")
        self.assertEqual(result.row_texts(), ["𠀀 e\u0301 👨‍👩‍👧 тест"])
        self.assertEqual(result.formatting.rows[0][0].text, "𠀀 e\u0301 👨‍👩‍👧")
        self.assertTrue(result.formatting.rows[0][0].underline)

    def test_example_pair_separator_is_unstyled_and_translations_keep_source_styles(self):
        result = article("[m1]привет[/m][m2][ex]你好 [i]добрый[/i] день[/ex][/m]")
        self.assertEqual(result.row_texts(), ["привет", "你好 — добрый день"])
        self.assertEqual([(run.text, run.italic) for run in result.formatting.rows[1]],
                         [("你好 — ", False), ("добрый", True), (" день", False)])

    def test_nested_examples_and_definitions_follow_client_row_order(self):
        result = article("[m1]1) [b]первое[/b][m2][*][ex]一 один[/ex][/*][/m][/m]"
                         "[m1]2) второе[/m][m2][ex]二 [i]два[/i][/ex][/m]")
        self.assertEqual(result.version, 1)
        self.assertEqual(result.row_texts(), ["первое", "一 — один", "второе", "二 — два"])
        self.assertTrue(result.formatting.rows[0][0].bold)
        self.assertEqual(result.senses[0].number, "1")
        self.assertFalse(any(run.text.startswith("1)") for run in result.formatting.rows[0]))

    def test_source_number_style_is_removed_with_number_not_applied_to_body(self):
        result = article("[m1][b]1)[/b] смысл[/m]")
        self.assertEqual(result.senses[0].number, "1")
        self.assertEqual(result.row_texts(), ["смысл"])
        self.assertIsNone(result.formatting)

    def test_default_color_and_labels_are_green_with_label_italic(self):
        result = article("[m1][p]вежл.[/p] [c]помета[/c] текст[/m]")
        runs = result.formatting.rows[0]
        self.assertEqual((runs[0].text, runs[0].italic, runs[0].color), ("вежл.", True, "green"))
        self.assertTrue(any(run.text == "помета" and not run.italic and run.color == "green" for run in runs))

    def test_actual_named_colors_have_closed_safe_mapping(self):
        for source, expected in {"red": "red", "blue": "blue", "green": "green",
                                 "gray": "gray", "grey": "gray", "black": "primary",
                                 "brown": "brown", "violet": "violet", "crimson": "crimson"}.items():
            with self.subTest(source=source):
                result = article(f"[m1][c {source.upper()}]цвет[/c][/m]")
                self.assertEqual(result.formatting.rows[0][0].color, expected)

    def test_unknown_color_degrades_only_color_and_keeps_other_style(self):
        result = article("[m1][i][c rgb(1,2,3)]текст[/c][/i][/m]")
        self.assertEqual(result.row_texts(), ["текст"])
        self.assertTrue(result.formatting.rows[0][0].italic)
        self.assertIsNone(result.formatting.rows[0][0].color)
        self.assertIsNone(article("[m1][c unknown]текст[/c][/m]").formatting)

    def test_plain_article_does_not_duplicate_text_in_new_payload(self):
        result = article("[m1]обычный текст[/m]")
        self.assertIsNone(result.formatting)
        self.assertNotIn("formatting", result.model_dump())
        self.assertNotIn("formatting", json.loads(result.model_dump_json()))

    def test_cache_raw_mismatch_keeps_existing_content_without_guessed_styles(self):
        source = "[m1][i]тот тот[/i][/m]"
        cached = parse_dsl_article("[m1]тот другой[/m]")
        before = copy.deepcopy(cached)
        result = projected(source, cached)
        self.assertEqual(result.article.senses[0].definition, "тот другой")
        self.assertIsNone(result.article.formatting)
        self.assertEqual(cached, before)

    def test_matching_text_but_different_reference_target_is_not_enriched(self):
        raw = '[m1][i]см.[/i] [ref target="甲"]слово[/ref][/m]'
        cached = parse_dsl_article(raw)
        cached["senses"][0]["references"][0]["target"] = "乙"
        result = projected(raw, cached)
        self.assertIsNone(result.article.formatting)
        self.assertEqual(result.article.senses[0].references[0].target, "乙")

    def test_invalid_raw_does_not_discard_valid_cached_article(self):
        cached = parse_dsl_article("[m1]прежний текст[/m]")
        result = projected("[m1][i]несовместимый[/m]", cached)
        self.assertEqual(result.article.model_dump(), cached)
        self.assertEqual(result.ru, "прежний текст")

    def test_large_raw_stays_readable_and_does_not_enter_style_cache(self):
        raw = "[m1][i]" + "а" * 65_536 + "[/i][/m]"
        semantic = DictionaryArticle.model_validate(parse_dsl_article(raw))
        before = _source_formatting.cache_info()
        result = enrich_article(semantic, raw)
        self.assertIsNone(result.formatting)
        self.assertEqual(result.senses[0].definition, "а" * 65_536)
        self.assertEqual(_source_formatting.cache_info(), before)

    def test_style_cache_is_bounded(self):
        for index in range(130):
            self.assertIsNotNone(article(f"[m1][i]слово {index}[/i][/m]").formatting)
        self.assertLessEqual(_source_formatting.cache_info().currsize, 128)


class FormattingValidationTests(unittest.TestCase):
    def test_bad_optional_metadata_never_drops_readable_article_or_mutates_cache(self):
        base = parse_dsl_article("[m1]текст[/m]")
        valid = {"version": 1, "rows": [[{"text": "текст", "italic": True}]]}
        invalid = ["bad", [], {}, {"version": True, "rows": valid["rows"]},
                   {"version": "1", "rows": valid["rows"]},
                   {"version": 2, "rows": valid["rows"]},
                   {"version": 1, "rows": []}, {"version": 1, "rows": [[], []]},
                   {"version": 1, "rows": [[{"text": "другой"}]]}]
        for alteration in ({"italic": "true"}, {"bold": 1}, {"underline": 0},
                           {"color": "orange"}, {"color": 3}, {"text": ""},
                           {"text": 1}, {"unknown": True}):
            invalid.append({"version": 1, "rows": [[dict(text="текст", **alteration)
                                                       if "text" not in alteration else alteration]]})
        for bad in invalid:
            with self.subTest(metadata=bad):
                cached = dict(base, formatting=bad)
                before = copy.deepcopy(cached)
                result = DictionaryArticle.model_validate(cached)
                self.assertEqual(result.model_dump(), base)
                self.assertIsNone(result.formatting)
                self.assertEqual(cached, before)

    def test_optional_metadata_round_trips_with_exact_row_text(self):
        result = article("[m1][i]один[/i] два[/m]")
        decoded = DictionaryArticle.model_validate_json(result.model_dump_json())
        self.assertEqual(decoded, result)
        self.assertEqual(decoded.formatting.version, 1)

    def test_empty_run_list_is_valid_only_for_empty_row(self):
        cached = {"version": 1, "senses": [{"definition": "", "references": [{"target": "甲", "label": "甲"}]}],
                  "formatting": {"version": 1, "rows": [[]]}}
        result = DictionaryArticle.model_validate(cached)
        self.assertIsNotNone(result.formatting)
        cached["senses"][0]["definition"] = "слово"
        self.assertIsNone(DictionaryArticle.model_validate(cached).formatting)


if __name__ == "__main__":
    unittest.main()
