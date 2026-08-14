"""Кнопки «Текст» и «Помощь»: разбор, кэш подсказки, защита от чужих id."""

from __future__ import annotations

import uuid

import pytest

from app.bot.keyboards.answer import ACTION_HELP, ACTION_TEXT, answer_keyboard, parse_action
from app.core.services import breakdown as bd
from app.core.services.pinyin import to_pinyin
from app.db.models import Dialog, User


class FakeSession:
    """Минимальная подмена сессии: get по id и счётчик flush."""

    def __init__(self, rows: dict[int, Dialog]) -> None:
        self._rows = rows
        self.added: list[object] = []

    async def get(self, _model, key):
        return self._rows.get(key)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def _user() -> User:
    user = User()
    user.id = uuid.uuid4()
    user.hsk_level = "hsk12"
    return user


def _reply(user: User, dialog_id: int = 1, **kwargs) -> Dialog:
    row = Dialog(user_id=user.id, role="assistant", text_zh="你好吗？", **kwargs)
    row.id = dialog_id
    return row


@pytest.mark.asyncio
async def test_текст_берётся_из_базы_без_платных_вызовов():
    """Главный критерий этапа: разбор уже сохранён, платить второй раз не за что."""
    user = _user()
    row = _reply(user, pinyin="nǐ hǎo ma?", translation="Как дела?")
    session = FakeSession({1: row})

    result = await bd.get_text_breakdown(session, user, 1)

    assert result.pinyin == "nǐ hǎo ma?"
    assert result.translation == "Как дела?"
    assert result.pinyin_offline is False


@pytest.mark.asyncio
async def test_пиньинь_считается_локально_если_модель_его_не_вернула():
    """Модель уже ломала JSON — кнопка обязана показать тоны и в этом случае."""
    user = _user()
    session = FakeSession({1: _reply(user, pinyin=None)})

    result = await bd.get_text_breakdown(session, user, 1)

    assert result.pinyin_offline is True
    assert "ǐ" in result.pinyin  # знаки тонов, а не цифры


@pytest.mark.asyncio
async def test_чужая_реплика_не_отдаётся():
    """callback_data приходит от клиента: подставить чужой id ничего не стоит."""
    свой, чужой = _user(), _user()
    session = FakeSession({1: _reply(чужой)})

    with pytest.raises(bd.ReplyNotFound):
        await bd.get_text_breakdown(session, свой, 1)


@pytest.mark.asyncio
async def test_подсказка_из_кэша_не_идёт_в_модель():
    user = _user()
    items = [bd.Suggestion(zh="我很好", pinyin="wǒ hěn hǎo", ru="У меня всё хорошо")]
    row = _reply(user, help_text=bd._encode(items))
    session = FakeSession({1: row})

    got, from_cache = await bd.get_suggestions(session, user, 1)

    assert from_cache is True
    assert got == items


def test_кэш_подсказки_переживает_кодирование():
    items = [
        bd.Suggestion(zh="我很好", pinyin="wǒ hěn hǎo", ru="Хорошо"),
        bd.Suggestion(zh="不太好", pinyin="bù tài hǎo", ru=None),
    ]
    assert bd._decode(bd._encode(items)) == items


def test_разбор_вариантов_отсеивает_мусор():
    data = {
        "suggestions": [
            {"zh": "我很好", "pinyin": "wǒ hěn hǎo", "ru": "Хорошо"},
            {"zh": "", "pinyin": "x", "ru": "пусто"},
            "не словарь",
            {"zh": "谢谢", "ru": "Спасибо"},  # пиньинь не пришёл — считаем сами
        ]
    }
    items = bd._parse_suggestions(data)
    assert [i.zh for i in items] == ["我很好", "谢谢"]
    assert items[1].pinyin == "xiè xiè"  # посчитан локально


def test_вариантов_не_больше_трёх():
    data = {"suggestions": [{"zh": f"话{i}"} for i in range(10)]}
    assert len(bd._parse_suggestions(data)) == bd.MAX_SUGGESTIONS


def test_мусор_в_начале_списка_не_съедает_подсказку():
    """Лимит считается по годным вариантам, иначе подсказка приходила бы пустой."""
    data = {"suggestions": ["мусор", {}, {"zh": ""}, {"zh": "好的", "ru": "Хорошо"}]}
    assert [i.zh for i in bd._parse_suggestions(data)] == ["好的"]


def test_сломанный_ответ_модели_не_роняет_подсказку():
    assert bd._parse_suggestions({"_raw": "модель написала прозой"}) == []


def test_локальный_пиньинь_со_знаками_тонов():
    assert to_pinyin("你好") == "nǐ hǎo"


def test_кнопки_разбираются_обратно():
    markup = answer_keyboard(42)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert parse_action(data[0]) == (ACTION_TEXT, 42)
    assert parse_action(data[1]) == (ACTION_HELP, 42)


def test_нажатая_кнопка_исчезает():
    """Так и обеспечивается «повторное нажатие не дублирует сообщения»."""
    markup = answer_keyboard(42, with_text=False)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["ans:help:42"]
    assert answer_keyboard(42, with_text=False, with_help=False) is None


def test_чужой_callback_data_не_разбирается():
    assert parse_action("hsk:hsk12") is None
    assert parse_action("ans:txt:не-число") is None
    assert parse_action("") is None


def test_угловые_скобки_экранируются():
    """Один «<» в переводе — и Telegram отвечает 400, сообщение не уходит вовсе."""
    from app.bot.render import esc

    assert esc("1 < 2 & 3") == "1 &lt; 2 &amp; 3"
    assert esc(None) == ""


def test_слово_null_не_становится_поправкой():
    """Регрессия: модель пишет отсутствие значения словом, а не JSON-значением.

    На живой проверке это дошло до юзера сообщением «Небольшая поправка: null».
    """
    from app.core.providers.base import _text_or_none

    assert _text_or_none("null") is None
    assert _text_or_none("None.") is None
    assert _text_or_none("нет") is None
    assert _text_or_none("  ") is None
    assert _text_or_none(None) is None
    # Осмысленный текст не должен пострадать.
    assert _text_or_none("Нет нужды в 了") == "Нет нужды в 了"
