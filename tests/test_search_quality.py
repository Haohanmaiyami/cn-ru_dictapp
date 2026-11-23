"""Source-based search regressions; no production settings are imported.

Pure tests always run. SQL tests require DICTAPP_SEARCH_TEST_URL pointing to a
throwaway localhost database whose name starts with dictapp_search_test.
They create and drop ONLY their private schema; never use DATABASE_URL or .env.
"""
import os
import re
import unittest
import uuid

from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from dictapp.models import Base, Entry
from dictapp.repo import clean_and_deduplicate_entries, russian_search_rank, search_entries
from dictapp.reverse_article import parse_reverse_article

# Unmodified article bodies from data/dabruks.dsl (250920), kept inline so tests
# remain reproducible without distributing/reading the full local dictionary.
REAL_REVERSE_BODIES = {'здравствуйте': '[m1]您好 nínhǎo, 你好 nǐhǎo; ([i]всем[/i]) 大家好 dàjiā hǎo[/m]',
 'кот': '[m1]1) 猫 māo, ([i]самец[/i]) 公猫 gōngmāo[/m][m2][*]- [ref]не всё коту масленица[/ref] - '
        '[ref]кот наплакал[/ref][/*][/m][m1]2) [p]сокр.[/p] [ref]который[/ref][/m]',
 'плохо': '[m1]1) [c][i]нареч.[/i][/c] 坏 huài, 不好 bùhǎo[/m][m2][*][ex]плохо обращаться с кем-либо '
          '- 对待...不好[/ex][/*][/m][m2][*][ex]он плохо работает - '
          '他工作得不好[/ex][/*][/m][m2][*][ex]плохо одетый - 穿得不好[/ex][/*][/m][m2][*][ex]плохо пахнуть '
          '- 有不好的气味; 有臭味[/ex][/*][/m][m2][*][ex]плохо чувствовать себя - '
          '觉得不舒服[/ex][/*][/m][m2][*][ex]плохо знать - 不大知道[/ex][/*][/m][m1]2) [c][i]в знач. сказ. '
          'безл.[/i][/c] 不好 bùhǎo, 不利 bùlì; ([i]с чем-либо[/i]) 缺乏 quēfá[/m][m2][*][ex]плохо с '
          'материалами - 材料缺乏[/ex][/*][/m][m1]3) [c][i]в знач. сказ. безл.[/i][/c] ([i]о '
          'физическом состоянии[/i]) 不好 bùhǎo, 不佳 bùjiā[/m][m2][*][ex]больному плохо - '
          '病人情况不佳[/ex][/*][/m][m1]4) [c][i]в знач. сущ. с нескл.[/i][/c] ([i]отметка[/i]) 不及格 '
          'bújígé, 不好 bùhǎo[/m][m1][*]•[/*][/m][m1][*]- [ref]плохо кончить[/ref][/*][/m]',
 'плохой': '[m1]坏\\[的\\] huài\\[de\\], 不好\\[的\\] bùhǎo\\[de\\]; 恶劣\\[的\\] èliè\\[de\\]; '
           '([i]низкосортный[/i]) 劣等\\[的\\] lièděng\\[de\\]; [p]разг.[/p] 很烂\\[的\\] hěnlàn\\[de\\] '
           '[/m][m2][*][ex]плохая погода - 不好的天气; 恶劣的天气[/ex][/*][/m][m2][*][ex]плохой табак - '
           '劣等烟草[/ex][/*][/m][m2][*][ex]плохой человек - 坏人[/ex][/*][/m][m2][*][ex]плохая привычка '
           '- 坏习惯[/ex][/*][/m][m2][*][ex]плохой характер - 坏脾气; '
           '不好的性格[/ex][/*][/m][m2][*][ex]плохое настроение - 不好的情绪[/ex][/*][/m][m2][*][ex]на него '
           'надежда плоха - 他是靠不住的人[/ex][/*][/m][m2][*]- [ref]с ним шутки плохи[/ref][/*][/m]',
 'привет': '[m1]1) 敬礼 jìnglǐ, 问候 wènhòu[/m][m2][*][ex]передайте ему привет - '
           '请代我问候他[/ex][/*][/m][m2][*][ex]вам привет от кого-либо - \\[有\\]... '
           '问候您[/ex][/*][/m][m2][*][ex]с приветом ([i]в конце письма[/i]) - '
           '此致敬礼[/ex][/*][/m][m1]2) ([i]приветствие[/i]) 你好 nǐhǎo[/m]',
 'приветик': '[m1][p]разг.[/p], [p]уменьш.-ласк. к[/p] [ref]привет[/ref][/m]',
 'приветствие': '[m1]1) 贺词 hècí, 祝贺词 zhùhècí; 欢迎词 huānyíngcí[/m][m2][*][ex]обратиться к кому-либо '
                'с приветствием - 向... 致贺词[/ex][/*][/m][m1]2) [c][i]воен.[/i][/c] 举手敬礼 jǔshǒu '
                'jìnglǐ[/m]'}


def reverse_entry(headword, *, entry_id=None, hanzi=None):
    body = REAL_REVERSE_BODIES[headword]
    if hanzi is None:
        match = re.search(r"[\u3400-\u9fff]+", body)
        hanzi = match.group(0) if match else ""
    article = parse_reverse_article(headword, body)
    assert article is not None
    return Entry(id=entry_id, hanzi=hanzi, pinyin=None, ru=headword,
                 examples=body, raw_dsl=body, article=article)


class SearchRankingTests(unittest.TestCase):
    def test_original_russian_articles_win_without_translation_overrides(self):
        for query, chinese in [("привет", "敬礼"), ("плохо", "坏")]:
            source = reverse_entry(query, entry_id=100)
            gloss = Entry(id=1, hanzi="你好", ru=query, pinyin="nǐ hǎo")
            self.assertEqual(source.hanzi, chinese)
            ranked = sorted([gloss, source], key=lambda entry: russian_search_rank(entry, query))
            self.assertIs(ranked[0], source)
            self.assertEqual(source.raw_dsl, REAL_REVERSE_BODIES[query])

    def test_same_first_chinese_translation_does_not_merge_russian_headwords(self):
        source = reverse_entry("плохо", entry_id=1)
        other = reverse_entry("плохой", entry_id=2, hanzi=source.hanzi)
        self.assertEqual(clean_and_deduplicate_entries([source, other, source], preserve_russian_articles=True), [source, other])

    def test_reference_only_source_article_survives_empty_hanzi(self):
        source = reverse_entry("приветик", entry_id=3)
        self.assertEqual(source.hanzi, "")
        self.assertEqual(clean_and_deduplicate_entries([source], preserve_russian_articles=True), [source])

    def test_identical_reimports_merge_but_distinct_source_bodies_do_not(self):
        original = reverse_entry("привет", entry_id=1)
        duplicate = reverse_entry("привет", entry_id=2)
        distinct = reverse_entry("привет", entry_id=3)
        distinct.raw_dsl += "\n[m1]другой источник[/m]"
        self.assertEqual(clean_and_deduplicate_entries([original, duplicate, distinct], preserve_russian_articles=True),
                         [original, distinct])

    def test_ai_context_prefers_original_chinese_article_over_reverse_alias(self):
        reverse = reverse_entry("привет", entry_id=1, hanzi="你好")
        chinese = Entry(id=2, hanzi="你好", ru="привет", pinyin="nǐ hǎo")
        self.assertEqual(clean_and_deduplicate_entries([reverse, chinese]), [chinese])

    def test_short_russian_gloss_is_not_assumed_to_be_a_source_headword(self):
        entry = Entry(id=1, hanzi="你好", ru="привет", article={"source": {"language": "ru"}})
        self.assertEqual(russian_search_rank(entry, "привет")[1], 3)

    def test_richness_is_not_penalized_for_homographs(self):
        short = Entry(id=1, hanzi="好", ru="хорошо")
        rich = Entry(id=2, hanzi="好", ru="хорошо; хорошее состояние", examples="пример")
        self.assertEqual(clean_and_deduplicate_entries([short, rich]), [rich])


TEST_URL = os.environ.get("DICTAPP_SEARCH_TEST_URL")


@unittest.skipUnless(TEST_URL, "set DICTAPP_SEARCH_TEST_URL to a throwaway localhost DB")
class SearchPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(TEST_URL)
        if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or not (url.database or "").startswith("dictapp_search_test")
                or url.query):
            raise RuntimeError("SQL tests require an explicit throwaway localhost test database")
        self.schema = "search_test_" + uuid.uuid4().hex
        self.engine = create_async_engine(url, connect_args={"server_settings": {"search_path": self.schema}})
        async with self.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            await connection.run_sync(Base.metadata.create_all)
        self.session = AsyncSession(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.session.close()
        async with self.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.engine.dispose()

    async def add(self, *entries):
        self.session.add_all(entries)
        await self.session.commit()

    async def test_russia_prefers_eluosi_before_country_pairs_and_eguo(self):
        await self.add(
            Entry(hanzi="俄白", ru="Россия и Белоруссия"),
            Entry(hanzi="俄国", ru="Россия; русский; российский"),
            Entry(hanzi="俄芬", ru="Россия и Финляндия; российско-финский"),
            Entry(hanzi="俄罗斯", pinyin="éluósī", ru="Россия, российский, русский"),
        )
        for query in ("россия", "Россия", " РОССИЯ "):
            results = await search_entries(self.session, query, limit=1)
            self.assertEqual([entry.hanzi for entry in results], ["俄罗斯"])
        pair = await search_entries(self.session, "Россия и Белоруссия", limit=1)
        self.assertEqual([entry.hanzi for entry in pair], ["俄白"])

    async def test_source_title_wins_and_rich_body_survives(self):
        for query in ["привет", "плохо"]:
            source = reverse_entry(query)
            await self.add(Entry(hanzi="短", ru=query), source)
            result = await search_entries(self.session, query, limit=1)
            self.assertEqual([entry.id for entry in result], [source.id])
            self.assertEqual(result[0].raw_dsl, REAL_REVERSE_BODIES[query])
            self.assertEqual(result[0].article["source"]["headword"], query)

    async def test_source_prefix_precedes_plain_translation(self):
        source = reverse_entry("приветствие")
        await self.add(Entry(hanzi="你好", ru="привет"), source)
        result = await search_entries(self.session, "привет")
        self.assertEqual(result[0].id, source.id)

    async def test_distinct_reverse_articles_with_same_hanzi_and_reference_only(self):
        first = reverse_entry("плохо")
        second = reverse_entry("плохой", hanzi=first.hanzi)
        reference = reverse_entry("приветик")
        await self.add(first, second, reference)
        result = await search_entries(self.session, "плох")
        self.assertEqual({entry.id for entry in result}, {first.id, second.id})
        self.assertEqual((await search_entries(self.session, "приветик"))[0].id, reference.id)

    async def test_deduplication_happens_before_limit(self):
        await self.add(*(Entry(hanzi="你好", ru="привет") for _ in range(12)),
                       Entry(hanzi="您好", ru="привет", examples="полная статья"),
                       Entry(hanzi="你好", ru="привет", examples="богатая статья"))
        result = await search_entries(self.session, "привет", limit=2)
        self.assertEqual({entry.hanzi for entry in result}, {"你好", "您好"})
        self.assertTrue(all(entry.examples for entry in result))

    async def test_like_metacharacters_are_literal(self):
        await self.add(Entry(hanzi="甲", ru="тест%_буквально"), Entry(hanzi="乙", ru="тестXYZбуквально"))
        result = await search_entries(self.session, "тест%_")
        self.assertEqual([entry.hanzi for entry in result], ["甲"])

    async def test_long_query_is_checked_beyond_prefix80(self):
        query = "длинный " * 12 + "хвост"
        await self.add(Entry(hanzi="甲", ru=query), Entry(hanzi="乙", ru=query[:80] + "другой"))
        result = await search_entries(self.session, query)
        self.assertEqual([entry.hanzi for entry in result], ["甲"])

    async def test_russian_fallback_matches_words_not_inside_words(self):
        await self.add(Entry(hanzi="猫", ru="домашний кот"), Entry(hanzi="谁", ru="тот который"),
                       Entry(hanzi="小猫", ru="маленький котик"))
        result = await search_entries(self.session, "кот")
        self.assertEqual([entry.hanzi for entry in result], ["猫"])

    async def test_direct_matches_are_not_topped_up_with_substrings(self):
        await self.add(Entry(hanzi="你好", ru="привет"), Entry(hanzi="甲", ru="передать привет"))
        result = await search_entries(self.session, "привет")
        self.assertEqual([entry.hanzi for entry in result], ["你好"])

    async def test_short_russian_query_does_not_scan_global_contains(self):
        await self.add(Entry(hanzi="甲", ru="редкое яя слово"))
        self.assertEqual(await search_entries(self.session, "яя"), [])

    async def test_chinese_exact_prefix_then_contains_only_when_no_direct_matches(self):
        await self.add(Entry(hanzi="你好", ru="привет"), Entry(hanzi="你好吗", ru="как дела"),
                       Entry(hanzi="向你好吗", ru="длинный пример"))
        result = await search_entries(self.session, "你好")
        self.assertEqual([entry.hanzi for entry in result], ["你好", "你好吗"])
        result = await search_entries(self.session, "好吗")
        self.assertEqual([entry.hanzi for entry in result], ["你好吗", "向你好吗"])

    async def test_pinyin_tones_spaces_case_and_numeric_notation(self):
        await self.add(Entry(hanzi="你好", pinyin="nǐ hǎo", ru="привет"),
                       Entry(hanzi="你好啊", pinyin="nǐ hǎo a", ru="приветствие"),
                       Entry(hanzi="宁", pinyin="níng", ru="спокойствие"))
        for query in ["nihao", "NI HAO", "nǐ hǎo", "ni3 hao3"]:
            result = await search_entries(self.session, query, limit=1)
            self.assertEqual([entry.hanzi for entry in result], ["你好"])

    async def test_accented_first_letter_and_original_initial_scope(self):
        await self.add(Entry(hanzi="安", pinyin="ān", ru="покой"), Entry(hanzi="你好", pinyin="nǐ hǎo", ru="привет"),
                       Entry(hanzi="哪年", pinyin="nǎ nián", ru="который год"))
        self.assertEqual([entry.hanzi for entry in await search_entries(self.session, "an")], ["安"])
        self.assertEqual(await search_entries(self.session, "hao"), [])
        self.assertEqual([entry.hanzi for entry in await search_entries(self.session, "nian")], ["哪年"])

    async def test_chinese_search_prefers_chinese_source_for_a_shared_hanzi(self):
        reverse = reverse_entry("привет", hanzi="你好")
        chinese = Entry(hanzi="你好", ru="привет", pinyin="nǐ hǎo")
        await self.add(reverse, chinese)
        self.assertEqual([entry.id for entry in await search_entries(self.session, "你好")], [chinese.id])

    async def test_identical_russian_reimports_deduplicate_before_limit(self):
        original = reverse_entry("привет")
        duplicate = reverse_entry("привет")
        distinct = reverse_entry("привет")
        distinct.raw_dsl += "\n[m1]другой источник[/m]"
        await self.add(original, duplicate, distinct)
        result = await search_entries(self.session, "привет", limit=2)
        self.assertEqual({entry.id for entry in result}, {original.id, distinct.id})

    async def test_pinyin_match_after_former_candidate_cutoff(self):
        # All filler rows share the old first-letter filter, but not this query.
        await self.session.execute(insert(Entry), [dict(hanzi="宁", pinyin="ning", ru="покой") for _ in range(50001)])
        wanted = Entry(hanzi="你好", pinyin="nǐ hǎo", ru="привет")
        await self.add(wanted)
        result = await search_entries(self.session, "nihao", limit=1)
        self.assertEqual([entry.id for entry in result], [wanted.id])

    async def test_malformed_provenance_cannot_promote_a_gloss(self):
        bad = Entry(hanzi="甲", ru="привет", article={"source": {"dictionary": "bruks", "language": "ru", "headword": ["привет"]}})
        source = reverse_entry("привет")
        await self.add(bad, source)
        result = await search_entries(self.session, "привет", limit=1)
        self.assertEqual(result[0].id, source.id)

    async def test_uppercase_russian_query_uses_case_insensitive_search(self):
        source = reverse_entry("привет")
        await self.add(source)
        self.assertEqual((await search_entries(self.session, "ПРИВЕТ"))[0].id, source.id)


if __name__ == "__main__":
    unittest.main()
