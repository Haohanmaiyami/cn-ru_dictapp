from sqladmin import ModelView
from dictapp.models import Entry


class EntryAdmin(ModelView, model=Entry):
    column_list = ["id", "hanzi", "pinyin", "pos"]    # какие колонки показывать в списке
    column_searchable_list = ["hanzi", "pinyin", "ru"]     # по каким полям искать в админке
    column_default_sort = ("id", True)  # True = asc     # сортировка по умолчанию
    page_size = 25     # сколько строк на странице
