"""Lossless ordered-block regression tests; pure parsing, no DB or network."""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from dictapp.dsl_parser import parse_dsl_article

FIXTURES = json.loads((ROOT / 'tests/fixtures/bkrs_hierarchy_articles.json').read_text())['articles']


def independent_plain(source):
    """Test oracle: strip only this fixture's known style/structural tags.

    Escape pairs are protected first so a literal \\[i\\] is never stripped.
    This is deliberately separate from production tokenization and rendering.
    """
    literals = []

    def protect(match):
        literals.append(match.group(1))
        return '\ue000' + str(len(literals) - 1) + '\ue001'

    value = re.sub(r'\\([\[\]{}\\])', protect, source)
    value = re.sub(r'\[/?(?:m[1-4]?|b|i|u|p|c|sup|sub|trn|com|t|\*|ex|ref)(?:\s+[^\]]*)?\]', '', value)
    value = re.sub(r'\ue000([0-9]+)\ue001', lambda m: literals[int(m.group(1))], value)
    return ' '.join(value.replace('\ufeff', '').replace('\u00a0', ' ').split())


def reconstruct(block):
    return ((block['number'] + ') ') if block['number'] else '') + block['text']


class HierarchyFixtureTests(unittest.TestCase):
    def test_all_original_screenshot_results_are_exactly_v1_compatible(self):
        expected = json.loads((ROOT / 'tests/fixtures/bkrs_v1_expected.json').read_text())['articles']
        original = json.loads((ROOT / 'tests/fixtures/bkrs_articles.json').read_text())['articles']
        self.assertEqual(set(expected), {row['head'] for row in original})
        for row in original:
            with self.subTest(head=row['head']):
                self.assertEqual(parse_dsl_article(row['raw_dsl']), expected[row['head']])
                self.assertEqual(expected[row['head']]['version'], 1)

    def test_exact_raw_sources_and_expected_support(self):
        self.assertEqual({row['head'] for row in FIXTURES}, set('着行了是一中好得的长散不'))
        for row in FIXTURES:
            with self.subTest(head=row['head']):
                self.assertRegex(row['source_file'], r'^dabkrs_[12]\.dsl$')
                self.assertGreater(row['source_ordinal'], 0)
                result = parse_dsl_article(row['raw_dsl'])
                if row['expected_version'] is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result['version'], row['expected_version'])

    def test_every_source_paragraph_preserves_text_level_order_and_example_kind(self):
        # These exact raw fixtures have sequential (not nested) paragraphs.
        # Each output block must correspond to one complete source paragraph.
        for row in FIXTURES:
            if row['expected_version'] is None:
                continue
            with self.subTest(head=row['head']):
                source_paragraphs = re.findall(r'\[m([1-4])\](.*?)\[/m\]', row['raw_dsl'], re.S)
                blocks = parse_dsl_article(row['raw_dsl'])['blocks']
                # 一 also has an explicit visible note after the last paragraph.
                self.assertEqual(len(blocks), len(source_paragraphs) + (row['head'] == '一'))
                for block, (level, paragraph) in zip(blocks, source_paragraphs):
                    self.assertEqual(block['level'], int(level))
                    self.assertEqual(block['kind'], 'example' if '[ex]' in paragraph else 'definition')
                    self.assertEqual(reconstruct(block), independent_plain(paragraph))
                    if block['kind'] == 'example':
                        self.assertIsNone(block['number'])
                    if block['chinese'] is not None:
                        self.assertEqual(block['text'], block['chinese'] + ' ' + block['translation'])

    def test_known_complex_layout_counts_and_nested_levels(self):
        for head, count in [('散', 56), ('好', 92), ('行', 96)]:
            row = next(row for row in FIXTURES if row['head'] == head)
            blocks = parse_dsl_article(row['raw_dsl'])['blocks']
            self.assertEqual(len(blocks), count)
            self.assertTrue(blocks[0]['text'].startswith('I'))
        for head in ['中', '的']:
            row = next(row for row in FIXTURES if row['head'] == head)
            blocks = parse_dsl_article(row['raw_dsl'])['blocks']
            self.assertIn(4, [block['level'] for block in blocks])

    def test_san_embedded_pinyin_is_retained_as_one_complete_example(self):
        raw = next(row['raw_dsl'] for row in FIXTURES if row['head'] == '散')
        block = next(block for block in parse_dsl_article(raw)['blocks'] if 'qīzǐ' in block['text'])
        self.assertEqual(block['kind'], 'example')
        self.assertEqual(block['text'], '妻子 (qīzǐ) 离散 жёны и дети разлучены и рассеяны по свету')
        self.assertIsNone(block['chinese'])
        self.assertIsNone(block['translation'])

    def test_cross_references_keep_labels_targets_and_placement(self):
        for row in FIXTURES:
            if row['expected_version'] is None:
                continue
            with self.subTest(head=row['head']):
                expected = [independent_plain(text) for text in re.findall(r'\[ref\](.*?)\[/ref\]', row['raw_dsl'], re.S)]
                blocks = parse_dsl_article(row['raw_dsl'])['blocks']
                actual = [reference['target'] for block in blocks for reference in block['references']]
                self.assertEqual(actual, expected)
                for block in blocks:
                    for reference in block['references']:
                        self.assertIn(reference['label'], block['text'])

    def test_unlevelled_pronunciation_note_does_not_disappear(self):
        raw = next(row['raw_dsl'] for row in FIXTURES if row['head'] == '一')
        self.assertIn('Примечание:', raw)
        blocks = parse_dsl_article(raw)['blocks']
        note = independent_plain(raw.rsplit('[/m]', 1)[1])
        self.assertEqual((blocks[-1]['level'], blocks[-1]['kind'], blocks[-1]['text']),
                         (1, 'definition', note))
        source = independent_plain(raw.split('\n', 1)[1])
        output = ''.join(reconstruct(block) for block in blocks)
        self.assertEqual(re.sub(r'\s+', '', source), re.sub(r'\s+', '', output))


class OrderedBlockSafetyTests(unittest.TestCase):
    def blocks(self, raw):
        article = parse_dsl_article(raw)
        self.assertIsNotNone(article)
        self.assertEqual(article['version'], 2)
        return article['blocks']

    def test_m2_without_ex_is_definition_including_cjk_text(self):
        blocks = self.blocks('[m1]I[/m][m2]1) 是 используется как связка[/m]')
        self.assertEqual([(b['level'], b['kind'], b['number'], b['text']) for b in blocks],
                         [(1, 'definition', None, 'I'), (2, 'definition', '1', '是 используется как связка')])

    def test_m3_definition_and_m4_example_retain_explicit_levels(self):
        blocks = self.blocks('[m2]1) определение[/m][m3]а) признак[/m][m4][ex]你好 привет[/ex][/m]')
        self.assertEqual([(b['level'], b['kind']) for b in blocks], [(2, 'definition'), (3, 'definition'), (4, 'example')])

    def test_nested_mixed_content_and_multiple_examples_keep_source_order(self):
        raw = ('[m1]до[*][ex]你好 привет[/ex]между[ex]再见 пока[/ex][/*]после'
               '[m2]подраздел[/m]хвост[/m]')
        blocks = self.blocks(raw)
        self.assertEqual([b['text'] for b in blocks], ['до', '你好 привет', 'между', '再见 пока', 'после', 'подраздел', 'хвост'])
        self.assertEqual([b['level'] for b in blocks], [1, 1, 1, 1, 1, 2, 1])

    def test_ambiguous_examples_never_guess_translation_ownership(self):
        for example in ['你好привет', '你好 hello', '你好 iPhone телефон', '你好 привет 再见 пока',
                        '已经九点了? 9 часов', '你好 — привет', '1) 你好 привет']:
            with self.subTest(example=example):
                blocks = self.blocks('[m2][ex]' + example + '[/ex][/m]')
                self.assertEqual(blocks[0]['text'], example)
                self.assertIsNone(blocks[0]['number'])
                # '1)' is part of the source Chinese phrase, not a sense number;
                # its pair may still be safe under the unchanged v1 boundary rule.
                if not example.startswith('1)'):
                    self.assertIsNone(blocks[0]['chinese'])
                    self.assertIsNone(blocks[0]['translation'])

    def test_reference_inside_example_keeps_link_and_whole_text(self):
        block = self.blocks('[m2][ex][ref target="你好"]问候[/ref] привет[/ex][/m]')[0]
        self.assertEqual(block['text'], '问候 привет')
        self.assertEqual(block['references'], [{'target': '你好', 'label': '问候'}])
        self.assertIsNone(block['chinese'])

    def test_only_style_tags_may_cross(self):
        blocks = self.blocks('[m1]1) [c][i]помета[/c] пояснение[/i][/m]')
        self.assertEqual(blocks[0]['text'], 'помета пояснение')
        for raw in ['[m1][ex]你好 привет[/m][/ex]', '[m1][ref]你好[/m][/ref]',
                    '[m1][ex]до[m2]значение[/m][/ex][/m]', '[m2][ref][ex]例[/ex][/ref][/m]',
                    '[m2][ref]一[ref]二[/ref][/ref][/m]', '[m2][i]нет закрытия[/m]']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_dsl_article(raw))
        for raw in ['[m1][i]значение[/m][/i]', '[i][m1]значение[/i][/m]']:
            with self.subTest(raw=raw):
                self.assertEqual(self.blocks(raw)[0]['text'], 'значение')

    def test_multi_pronunciation_prefixes_and_missing_pinyin(self):
        for prefix in ['sàn, sǎn', 'zhōng; zhòng', 'dé, děi, de', '_, nǐ']:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.blocks(prefix + '\n[m2]значение[/m]')[0]['text'], 'значение')
        for prefix in ['sàn,', ',sǎn', 'sàn,,sǎn', 'sàn; примечание', '_ nǐ', '汉字', 'русский']:
            with self.subTest(prefix=prefix):
                self.assertEqual([b['text'] for b in self.blocks(prefix + '\n[m2]значение[/m]')],
                                 [prefix, 'значение'])

    def test_unknown_structures_hidden_text_and_trailing_prose_fall_back(self):
        for raw in ['[m5]значение[/m]', '[m2]{{комментарий}} значение[/m]', '[m2][url]ссылка[/url][/m]',
                    '[m2][ref dict="other"]字[/ref][/m]', '[m2]1)[/m]',
                    '[m2]' + '[i]' * 70 + 'текст' + '[/i]' * 70 + '[/m]']:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_dsl_article(raw))

    def test_literal_braces_loose_note_and_root_example_preserve_complete_text(self):
        for raw, expected in [('[m2]{буквально} значение[/m]', ['{буквально} значение']),
                              ('[m2]значение[/m]Примечание', ['значение', 'Примечание']),
                              ('[m2]значение[/m][ex]字 пример без уровня[/ex]',
                               ['значение', '字 пример без уровня'])]:
            with self.subTest(raw=raw):
                self.assertEqual([b['text'] for b in self.blocks(raw)], expected)

    def test_escape_sequences_and_literal_brackets_survive_v2(self):
        block = self.blocks(r'[m2]буквально \[i\], \{текст\}, C:\\tmp, \q[/m]')[0]
        self.assertEqual(block['text'], r'буквально [i], {текст}, C:\tmp, \q')

    def test_non_ascii_numbers_are_preserved_literally(self):
        block = self.blocks('[m2]١) значение[/m]')[0]
        self.assertIsNone(block['number'])
        self.assertEqual(block['text'], '١) значение')

    def test_decimal_text_is_not_rewritten_as_a_sense_number(self):
        block = self.blocks('[m2]1.5 метра[/m]')[0]
        self.assertIsNone(block['number'])
        self.assertEqual(block['text'], '1.5 метра')
        numbered = self.blocks('[m2]1. значение[/m]')[0]
        self.assertEqual(numbered['number'], '1')
        self.assertEqual(numbered['text'], 'значение')


if __name__ == '__main__':
    unittest.main()
