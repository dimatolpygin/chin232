"""Секреты не должны попадать в логи никогда."""

from __future__ import annotations

from app.logging import configure_logging, get_logger, mask_secrets


def test_значение_по_ключу_маскируется():
    result = mask_secrets(None, "info", {"bot_token": "123:AAreal_secret_value"})
    assert result["bot_token"] == "***"


def test_ключ_в_тексте_маскируется():
    result = mask_secrets(None, "info", {"event": "ответ sk-or-v1-abcdef0123456789 от сервиса"})
    assert "abcdef0123456789" not in result["event"]
    assert "sk-or***" in result["event"]


def test_токен_бота_в_тексте_маскируется():
    result = mask_secrets(
        None, "info", {"event": "url https://api.telegram.org/bot123456789:AAabcdefghij/send"}
    )
    assert "abcdefghij" not in result["event"]


def test_обычный_текст_не_трогаем():
    result = mask_secrets(None, "info", {"event": "входящее сообщение от @user"})
    assert result["event"] == "входящее сообщение от @user"


def test_секрет_из_трейсбека_маскируется(capsys):
    """Самый коварный случай: исключение с токеном внутри.

    Поле «ошибка» маскировалось и раньше, а вот traceback рисуется отдельным
    процессором — и если маскировка стоит выше него, в лог уезжает полный
    адрес файлового сервера Telegram вместе с токеном бота.
    """
    configure_logging("INFO", "json")
    log = get_logger("тест")
    try:
        raise RuntimeError(
            "Client error '404 Not Found' for url "
            "'https://api.telegram.org/file/bot123456789:AAsecrettokenvalue/voice/f.oga'"
        )
    except RuntimeError as exc:
        log.exception("круг оборван ошибкой", ошибка=repr(exc))

    вывод = capsys.readouterr().out
    assert "AAsecrettokenvalue" not in вывод
    assert "круг оборван ошибкой" in вывод
    # Трейсбек остаётся на месте: маскируется секрет, а не отладка.
    assert "Traceback" in вывод
