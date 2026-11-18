"""Actual fallback articles and independent source-content conservation checks."""
from pathlib import Path
import json
import re
import unittest

from dictapp.dsl_parser import _parse_v1, _parse_v2, parse_dsl_article
from dictapp.dsl_recovery import recovery_source


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "stage9_recovery.json").read_text())


def visible_source(text):
    """Independent text oracle: mask escapes before removing known DSL tags.

    This does not call the recovery tokenizer, helpers or semantic parser.
    Escaped literal [i] therefore remains text instead of being stripped.
    """
    escaped = []

    def mask(match):
        escaped.append(match.group(1))
        return f"\ue000{len(escaped) - 1}\ue001"

    text = re.sub(r"\\([\\\[\]{}])", mask, text)
    text = re.sub(r"\[/?(?:m[1-4]?|ex|ref|b|i|u|p|c|sup|sub|trn|com|t|\*)(?:\s+[^\]]*)?\]", "", text)
    return re.sub(r"\ue000(\d+)\ue001", lambda match: escaped[int(match.group(1))], text)


def compact(text):
    return "".join(char for char in text if not char.isspace() and char != "\ufeff")


class DSLRecoveryTests(unittest.TestCase):
    def blocks(self, raw):
        result = parse_dsl_article(raw)
        self.assertIsNotNone(result, raw)
        self.assertEqual(result["version"], 2)
        return result["blocks"]

    def test_real_fallbacks_have_manually_reviewed_rows(self):
        for row in FIXTURES:
            with self.subTest(hanzi=row["hanzi"]):
                self.assertIsNone(_parse_v1(row["raw_dsl"]))
                self.assertIsNone(_parse_v2(row["raw_dsl"]))
                blocks = self.blocks(row["raw_dsl"])
                self.assertEqual([block["text"] for block in blocks], row["expected_texts"])

    def test_real_fallbacks_conserve_visible_characters_in_source_order(self):
        for row in FIXTURES:
            with self.subTest(hanzi=row["hanzi"]):
                raw = row["raw_dsl"]
                body = raw.split("\n", 1)[1] if row["header_removed"] else raw
                expected = visible_source(body)
                actual = "".join((block["number"] + ")" if block["number"] else "") + block["text"]
                                 for block in self.blocks(raw))
                self.assertEqual(compact(actual), compact(expected))

    def test_real_references_keep_exact_labels_and_targets(self):
        for row in FIXTURES:
            expected = [visible_source(label) for label in re.findall(r"\[ref\](.*?)\[/ref\]", row["raw_dsl"], re.S)]
            actual = [reference for block in self.blocks(row["raw_dsl"]) for reference in block["references"]]
            self.assertEqual(actual, [{"label": label, "target": label} for label in expected])

    def test_literal_braces_unknown_escapes_and_escaped_tags_survive(self):
        raw = "_\n" + r"[m1]{拉} \[i\] \{已转义\} C:\\tmp \q[/m]"
        self.assertEqual(self.blocks(raw)[0]["text"], r"{拉} [i] {已转义} C:\tmp \q")
        escaped = recovery_source("_\n" + r"[m1]\{\{literal\}\}[/m]")
        self.assertEqual(_parse_v2(escaped)["blocks"][0]["text"], "{{literal}}")

    def test_only_first_confident_pronunciation_line_is_omitted(self):
        for first in ["nǐ", "nǐ; hǎo", "_, nǐ", "ni\u030c", "_"]:
            with self.subTest(first=first):
                self.assertEqual([block["text"] for block in self.blocks(first + "\nсмысл")], ["смысл"])
        for first in ["plain English prose", "manage", "ni", "ni3", "123", "nǐ,", "nǐ;", "__",
                      "_ nǐ", "nǐ (вариант)", "汉字", "русский", "nǐ; untoned"]:
            with self.subTest(first=first):
                canonical = recovery_source(first + "\n[m1]смысл[/m]")
                # Existing parsing precedence may intentionally accept some
                # plain Latin prefixes. Verify recovery's stricter path alone.
                parsed = _parse_v2(canonical)
                self.assertEqual([block["text"] for block in parsed["blocks"]], [first, "смысл"])

    def test_annotated_first_line_and_later_pinyin_are_visible(self):
        raw = "nǐ [i](вариант)[/i]\n[m1]смысл[/m]\nhǎo"
        self.assertEqual([block["text"] for block in self.blocks(raw)], ["nǐ (вариант)", "смысл", "hǎo"])

    def test_root_prose_links_examples_and_levels_keep_order(self):
        raw = ('_\nдо [ref target="你"]ты[/ref][m2]смысл[m4]подраздел[/m]хвост[/m]'
               '[ex]你好 — hello[/ex]после')
        blocks = self.blocks(raw)
        self.assertEqual([(block["level"], block["kind"], block["text"]) for block in blocks],
                         [(1, "definition", "до ты"), (2, "definition", "смысл"),
                          (4, "definition", "подраздел"), (2, "definition", "хвост"),
                          (1, "example", "你好 — hello"), (1, "definition", "после")])
        self.assertEqual(blocks[0]["references"], [{"target": "你", "label": "ты"}])
        self.assertIsNone(blocks[4]["chinese"])
        self.assertIsNone(blocks[4]["translation"])

    def test_cross_paragraph_styles_are_paired_without_reordering_words(self):
        raw = '_\n[m1][c brown][i]первый[/m][m2]второй[/c] третий[/i][/m]'
        canonical = recovery_source(raw)
        self.assertIsNotNone(_parse_v2(canonical))
        self.assertEqual([block["text"] for block in self.blocks(raw)], ["первый", "второй третий"])
        self.assertIn('[c brown][i]первый[/i][/c]', canonical)
        self.assertIn('[c brown][i]второй[/i][/c][i] третий[/i]', canonical)

    def test_balanced_style_may_enclose_a_whole_reference(self):
        raw = '_\n[i]до [ref target="甲"]字[/ref][m1]после[/m][/i]'
        blocks = self.blocks(raw)
        self.assertEqual([block["text"] for block in blocks], ["до 字", "после"])
        self.assertEqual(blocks[0]["references"], [{"target": "甲", "label": "字"}])

    def test_only_truly_empty_containers_are_omitted(self):
        raw = '_\n[m1][i][/m][m2]значение[/m][m1][/i][/m][m2][ex]  [/ex][/m]'
        self.assertEqual([block["text"] for block in self.blocks(raw)], ["значение"])
        for raw in ['_', '_\n[m1][/m]', '_\n[m1][ex][/ex][/m]', 'plain unstructured text']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_dsl_article(raw))

    def test_empty_or_invalid_references_are_never_discarded_as_spacing(self):
        for reference in ['[ref][/ref]', '[ref target="你"] [/ref]', '[ref target=""]字[/ref]',
                          '[ref dict="other"] [/ref]', '[ref target="你"][i][/i][/ref]']:
            with self.subTest(reference=reference):
                raw = '_\n[m1]' + reference + '[/m][m1]значение[/m]'
                self.assertIsNone(recovery_source(raw))
                self.assertIsNone(parse_dsl_article(raw))

    def test_comments_unknown_tags_and_unsupported_levels_remain_fallback(self):
        for body in ['[m1]{{hidden}} текст[/m]',
                     '[m1]текст }}[/m]', '[m1][url]link[/url][/m]', '[m1][s]audio.wav[/s][/m]',
                     '[m5]текст[/m]', '[m1][lang id=1033]text[/lang][/m]', '[m]текст[/m]',
                     '[m1][ref dict="other"]字[/ref][/m]']:
            with self.subTest(body=body):
                self.assertIsNone(parse_dsl_article('_\n' + body))
        # The established parser treats unmatched ordinary bracketed prose
        # literally. Recovery itself must not newly expose a hidden comment.
        self.assertIsNone(recovery_source('_\n[m1][{{hidden}}] текст[/m]'))

    def test_structural_crossing_and_unpaired_styles_are_not_guessed(self):
        for body in ['[m1]текст', '[m1]текст[/m][/m]', '[m1][ref]字[/m][/ref]',
                     '[m1][ex]字[/m][/ex]', '[m1][i]текст[/m]', '[m1]текст[/i][/m]',
                     '[m1][i][ref]字[/i][/ref][/m]', '[m1][ref][i]字[/ref][/i][/m]',
                     '[m1][ex][i]字[/ex][/i][/m]', '[m1][ref][ref]字[/ref][/ref][/m]',
                     '[m1][ex][m2]字[/m][/ex][/m]']:
            with self.subTest(body=body):
                self.assertIsNone(parse_dsl_article('_\n' + body))

    def test_depth_limit_still_rejects_pathological_input(self):
        self.assertIsNone(parse_dsl_article('_\n[m1]' + '[i]' * 70 + 'текст' + '[/i]' * 70 + '[/m]'))

    def test_established_contracts_keep_precedence_and_exact_payload(self):
        supported = ['nǐ\n[m1]ты, твой[/m]',
                     '[m1]1) привет[/m][m2][ex]你好 привет[/ex][/m]',
                     '[m1][i]см.[/i] [ref target="你"]ты[/ref][/m]',
                     '[m1]1) [c][i]помета[/c] текст[/i][/m]',
                     '[m2]1.5 метра[/m]', '[m2][ex]你好 hello[/ex][/m]']
        for raw in supported:
            with self.subTest(raw=raw):
                old = _parse_v1(raw) or _parse_v2(raw)
                self.assertIsNotNone(old)
                self.assertEqual(parse_dsl_article(raw), old)


if __name__ == '__main__':
    unittest.main()
