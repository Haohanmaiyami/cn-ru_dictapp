"""Article regressions from the user's screenshots, with no DB/network activity."""
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import tempfile
import types
from typing import Iterator, Optional
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dictapp.dsl_parser import parse_dsl_article

FIXTURES = {row["head"]: row for row in json.loads(
    (ROOT / "tests/fixtures/bkrs_articles.json").read_text())['articles']}


def article(word):
    result = parse_dsl_article(FIXTURES[word]["raw_dsl"])
    if result is None:
        raise AssertionError(f"Fixture {word} did not parse")
    return result


def old_split(text):
    # Execute only the existing pure function from its AST. Importing repo.py
    # would initialize DB settings and is unnecessary for this offline proof.
    path = ROOT / "src/dictapp/repo.py"
    module = ast.parse(path.read_text())
    func = next(node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "split_ru_examples")
    ns = {"re": re}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), ns)
    return ns["split_ru_examples"](text)


def old_import_clean(body):
    # The fixture's exact raw DSL goes through the existing cleaner, without
    # importing the full importer or giving it a database session.
    path = ROOT / "scripts/import_dsl.py"
    module = ast.parse(path.read_text())
    func = next(node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "clean_dsl_text")
    ns = {"re": re, "_TAG_RE": re.compile(r"\[/?[A-Za-z0-9*]+\]"),
          "_BRACE_RE": re.compile(r"\{[^}]*\}")}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), ns)
    # The first line is pinyin, removed by the existing importer.
    return ns["clean_dsl_text"](body).split("\n", 1)[1]


class ScreenshotRegressionTests(unittest.TestCase):
    def test_nihao_example_translation_stays_paired(self):
        sense = article("你好")["senses"][0]
        self.assertEqual(sense["definition"], "привет, здравствуй (стандартное приветствие)")
        self.assertEqual(sense["examples"], [{"chinese": "王女士，你好！",
                                             "translation": "Добрый день, госпожа Ван!"}])

    def test_ni_examples_have_separate_rows_and_literal_brackets(self):
        sense = article("你")["senses"][0]
        self.assertEqual(sense["definition"], "ты, твой")
        self.assertEqual(sense["examples"], [
            {"chinese": "你我两个人", "translation": "мы с тобой [вдвоём]"},
            {"chinese": "你家里", "translation": "в твоей семье, у тебя дома"}])

    def test_darao_examples_belong_to_second_meaning(self):
        senses = article("打扰")["senses"]
        self.assertEqual([s["number"] for s in senses], ["1", "2"])
        self.assertEqual([s["definition"] for s in senses],
                         ["тревожить, беспокоить", "вежл. простите за беспокойство"])
        self.assertEqual(senses[0]["examples"], [])
        self.assertEqual([e["chinese"] for e in senses[1]["examples"]],
                         ["打扰你了", "打扰一下"])

    def test_shenme_all_seven_senses_and_example_ownership(self):
        senses = article("什么")["senses"]
        self.assertEqual([s["number"] for s in senses], list("1234567"))
        self.assertEqual([len(s["examples"]) for s in senses], [3, 3, 2, 1, 1, 1, 1])
        self.assertEqual(senses[3]["definition"], "(после глагола) из-за чего?, зачем?")
        self.assertEqual(senses[3]["examples"], [{"chinese": "他们闹什么?",
                                                "translation": "из-за чего они шумят?"}])
        self.assertEqual(senses[1]["examples"][1],
                         {"chinese": "... 什么... 什么",
                          "translation": "что..., то [и]...; каков..., таков [и]"})

    def test_long_chinese_clauses_remain_intact(self):
        ex = article("什么")["senses"][4]["examples"][0]
        self.assertEqual(ex["chinese"], "什么送个信儿啊, 跑个腿儿啊, 我多能干")
        self.assertTrue(ex["translation"].startswith("такие поручения, как:"))
        self.assertIn("Ведь обходился же я до сих пор!",
                      article("什么")["senses"][5]["examples"][0]["translation"])

    def test_reference_is_a_link_and_not_an_example(self):
        sense = article("妈逼")["senses"][0]
        self.assertEqual(sense["definition"], "см. 妈屄")
        self.assertEqual(sense["examples"], [])
        self.assertEqual(sense["references"], [{"target": "妈屄", "label": "妈屄"}])
        self.assertTrue(article("妈屄")["senses"][0]["definition"])

    def test_existing_pipeline_reproduces_nihao_bug(self):
        translation, examples = old_split(old_import_clean(FIXTURES["你好"]["raw_dsl"]))
        self.assertIn("Добрый день, госпожа Ван!", translation)
        self.assertEqual(examples, "王女士，你好！")

    def test_existing_pipeline_reproduces_shenme_bug(self):
        translation, examples = old_split(old_import_clean(FIXTURES["什么"]["raw_dsl"]))
        self.assertNotIn("4)", translation)
        self.assertIn("4) (после глагола)", examples)
        self.assertIn("что, уже 9 часов?", translation)
        self.assertNotIn("что, уже 9 часов?", examples)


class ConservativeParsingTests(unittest.TestCase):
    def parse(self, body):
        result = parse_dsl_article(body)
        self.assertIsNotNone(result)
        return result["senses"]

    def test_missing_pinyin_marker_preserves_definition_language_and_text(self):
        # Actual raw-corpus pattern, including English-only definitions.
        for definition in ["polyaluminium chloride; polyaluminum chloride",
                           "подсеть, фрагмент сети", "字义", "имя_поля"]:
            with self.subTest(definition=definition):
                senses = self.parse('_\n[m1]' + definition + '[/m]')
                self.assertEqual(senses, [{"number": None, "definition": definition,
                                          "examples": [], "references": []}])

    def test_missing_pinyin_marker_preserves_sense_and_example_ownership(self):
        body = '[m1]1) первый[/m][m1]2) второй[/m][m2][ex]你好 привет[/ex][/m]'
        self.assertEqual(parse_dsl_article(' \t_\n' + body), parse_dsl_article(body))

    def test_uncertain_prefix_is_visible_and_only_exact_missing_marker_is_removed(self):
        for prefix in ['__', '_ nǐ', 'nǐ _', '_ примечание', '_\n_']:
            with self.subTest(prefix=prefix):
                blocks = parse_dsl_article(prefix + '\n[m1]значение[/m]')['blocks']
                visible_prefix = '_' if prefix == '_\n_' else prefix
                self.assertEqual([b['text'] for b in blocks], [visible_prefix, 'значение'])
        self.assertIsNone(parse_dsl_article('_'))
        self.assertEqual(self.parse('[m1]_[/m]')[0]['definition'], '_')

    def test_missing_pinyin_does_not_relax_unsupported_layout_checks(self):
        for body in ['[m1]значение[/m][m5]подуровень[/m]',
                     '[m1]{{скрыто}} значение[/m]',
                     '[m1]значение[/m][m2][ex]你好 привет[/m][/ex]']:
            with self.subTest(body=body):
                self.assertIsNone(parse_dsl_article('_\n' + body))

    def test_escaped_literal_tag_is_never_interpreted_as_markup(self):
        sense = self.parse(r"[m1]буквально \[i\] и \[вдвоём\], путь C:\\tmp[/m]")[0]
        self.assertEqual(sense["definition"], r"буквально [i] и [вдвоём], путь C:\tmp")

    def test_unknown_backslash_escape_is_preserved(self):
        self.assertEqual(self.parse(r"[m1]строка \q[/m]")[0]["definition"], r"строка \q")

    def test_escaped_braces_and_brackets_are_literal(self):
        self.assertEqual(self.parse(r"[m1]\{текст\} \[x\][/m]")[0]["definition"], "{текст} [x]")

    def test_inline_formatting_preserves_text(self):
        sense = self.parse('[m1]1) [b]текст[/b] [c green][i]помета[/i][/c] [sup]2[/sup][/m]')[0]
        self.assertEqual(sense["number"], "1")
        self.assertEqual(sense["definition"], "текст помета 2")

    def test_reference_label_and_target_can_differ(self):
        sense = self.parse('[m1][p]см.[/p] [ref target="你好"]приветствие[/ref][/m]')[0]
        self.assertEqual(sense["definition"], "см. приветствие")
        self.assertEqual(sense["references"], [{"target": "你好", "label": "приветствие"}])

    def test_nested_example_container_and_adjacent_senses(self):
        senses = self.parse('[m1]1) первый[m2][*][ex]你好 привет[/ex][/*][/m][/m]'
                            '[m1]2) второй[/m][m2][ex]再见 пока[/ex][/m]')
        self.assertEqual([s["number"] for s in senses], ["1", "2"])
        self.assertEqual([s["examples"][0]["translation"] for s in senses], ["привет", "пока"])

    def test_parenthesized_translation_keeps_opening_bracket(self):
        sense = self.parse('[m1]определение[/m][m2][ex]你好 (привет)[/ex][/m]')[0]
        self.assertEqual(sense["examples"], [{"chinese": "你好", "translation": "(привет)"}])

    def test_quoted_translation_keeps_its_quotes(self):
        sense = self.parse('[m1]определение[/m][m2][ex]你好 «привет»[/ex][/m]')[0]
        self.assertEqual(sense["examples"], [{"chinese": "你好", "translation": "«привет»"}])

    def test_numeric_and_separate_punctuation_prefixes_do_not_change_ownership(self):
        for example in ['已经九点了? 9 часов', '你好 — привет', '你好 ( привет)',
                        '你好 « привет »', '你好 3D модель']:
            with self.subTest(example=example):
                parsed = parse_dsl_article(
                    '[m1]определение[/m][m2][ex]' + example + '[/ex][/m]')
                self.assertEqual(parsed['version'], 2)
                self.assertEqual(parsed['blocks'][1]['text'], example)
                self.assertIsNone(parsed['blocks'][1]['chinese'])
                self.assertIsNone(parsed['blocks'][1]['translation'])

    def test_unsupported_or_ambiguous_articles_return_none(self):
        bodies = [
            '', 'plain unstructured text', '[m1]нет закрытия', '[m1]ошибка[/ex]',
            '[m1][unknown]текст[/unknown][/m]', '[m5]подуровень[/m]',
            '[m1]текст {{скрытый текст}}[/m]',
            '[m1][ref dict="another"]你好[/ref][/m]',
            '[m1][ref][i][ref]你好[/ref][/i][/ref][/m]',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(parse_dsl_article(body))

    def test_loose_prose_and_single_braces_are_preserved_in_order(self):
        for body, expected in [('[m1]текст[/m]Потерянный хвост', ['текст', 'Потерянный хвост']),
                               ('русский префикс[m1]значение[/m]', ['русский префикс', 'значение']),
                               ('[m1]текст {буквальные скобки}[/m]', ['текст {буквальные скобки}'])]:
            with self.subTest(body=body):
                parsed = parse_dsl_article(body)
                self.assertEqual(parsed['version'], 2)
                self.assertEqual([b['text'] for b in parsed['blocks']], expected)

    def test_whitespace_normalization_keeps_semantic_blocks(self):
        senses = self.parse('nǐ\n [m1] 1) ты\u00a0, твой [/m]\n[m1]2) второй[/m]')
        self.assertEqual([s["definition"] for s in senses], ["ты , твой", "второй"])

    def test_deeply_nested_input_falls_back_without_recursion_error(self):
        self.assertIsNone(parse_dsl_article('[m1]' + '[i]' * 100 + 'текст' + '[/i]' * 100 + '[/m]'))

    def test_fixtures_record_each_direct_source(self):
        self.assertEqual(set(FIXTURES), {'你好', '你', '打扰', '什么', '妈逼', '妈屄'})
        self.assertEqual({row['source_file'] for row in FIXTURES.values()},
                         {'dabkrs_1.dsl', 'dabkrs_2.dsl', 'dabkrs_3.dsl'})


class AdditiveImporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the actual pure importer definitions, excluding its database
        # imports and async writers. This test cannot connect or import data.
        path = ROOT / "scripts/import_dsl.py"
        source = ast.parse(path.read_text())
        pure_names = {
            'has_cjk', 'has_cyrillic', 'looks_like_pinyin', 'normalize_head',
            'clean_dsl_text', 'dsl_to_html', 'iter_dsl_articles',
            'extract_first_hanzi', 'extract_pinyin_after_hanzi',
            'extract_first_pinyin_line', 'split_pinyin_and_translation',
            'iter_entries_for_file',
        }
        nodes = [node for node in source.body
                 if (isinstance(node, ast.FunctionDef) and node.name in pure_names)
                 or (isinstance(node, ast.ClassDef) and node.name == 'ParsedEntry')
                 or (isinstance(node, ast.Assign) and
                     isinstance(node.targets[0], ast.Name) and
                     node.targets[0].id.endswith('_RE'))]
        cls.module_name = '_offline_dsl_importer_for_tests'
        module = types.ModuleType(cls.module_name)
        sys.modules[cls.module_name] = module
        module.__dict__.update(re=re, Path=Path, Optional=Optional, Iterator=Iterator,
                               dataclass=dataclass, parse_dsl_article=parse_dsl_article)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
        cls.importer = module

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.module_name, None)

    def import_one(self, raw, name='dabkrs_fixture.dsl', head='你好'):
        with tempfile.TemporaryDirectory(prefix='chinese-bridge-dsl-test-') as directory:
            path = Path(directory) / name
            path.write_text(head + '\n\t' + raw.replace('\n', '\n\t') + '\n', encoding='utf-16')
            rows = list(self.importer.iter_entries_for_file(path))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_new_bkrs_fields_leave_legacy_values_intact(self):
        for head, fixture in FIXTURES.items():
            with self.subTest(head=head):
                raw = fixture['raw_dsl']
                row = self.import_one(raw, head=head)
                self.assertEqual(row.raw_dsl, raw)
                self.assertEqual(row.article, article(head))
                self.assertEqual(row.ru_head, old_import_clean(raw))
                self.assertEqual(row.ru_full, self.importer.dsl_to_html(raw))

    def test_unsupported_layout_keeps_raw_and_legacy(self):
        raw = 'nǐ hǎo\n[m1]определение {{скрыто}}[/m][m2][ex]你好 9 часов[/ex][/m]'
        row = self.import_one(raw)
        self.assertIsNone(row.article)
        self.assertEqual(row.raw_dsl, raw)
        self.assertEqual(row.ru_head, old_import_clean(raw))
        self.assertEqual(row.ru_full, self.importer.dsl_to_html(raw))

    def test_missing_pinyin_keeps_raw_and_legacy_import_fields(self):
        raw = '_\n[m1]polyaluminium chloride; polyaluminum chloride[/m]'
        row = self.import_one(raw, head='聚氯化铝')
        self.assertEqual(row.raw_dsl, raw)
        # The legacy importer retains '_' because it is not actual pinyin.
        self.assertEqual(row.ru_head, '_\npolyaluminium chloride; polyaluminum chloride')
        self.assertEqual(row.ru_full, self.importer.dsl_to_html(raw))
        self.assertIsNotNone(row.article)
        self.assertEqual(row.article['senses'][0]['definition'],
                         'polyaluminium chloride; polyaluminum chloride')

    def test_bruks_keeps_existing_semantics_without_bkrs_structure(self):
        row = self.import_one('[m1]你好 nǐ hǎo привет[/m]', name='dabruks_fixture.dsl', head='привет')
        self.assertEqual(row.ru_head, 'привет')
        self.assertEqual(row.hanzi, '你好')
        self.assertIsNone(row.raw_dsl)
        self.assertIsNone(row.article)


if __name__ == '__main__':
    unittest.main()
