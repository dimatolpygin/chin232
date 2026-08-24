"""Копии базы: имена, срок хранения, разговор с хранилищем.

Главное, что здесь проверяется, — уборка. Бакет у хранилища общий с другими
проектами, и ошибка в правиле удаления стоила бы не наших файлов. Поэтому
`keep_plan` вынесен в чистую функцию и проверяется по датам, а не «на живом».
"""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime

import httpx
import pytest

from app.config import Settings
from app.core.providers.base import ProviderError
from app.core.providers.storage.s3 import S3Storage, StoredObject
from app.core.services.backup import (
    BackupStatus,
    backup_name,
    keep_plan,
    parse_day,
    prune,
)

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://china_bot:pass@postgres/china_bot",
    "redis_url": "redis://localhost:6379/5",
    "s3_endpoint": "https://s3.example.cloud",
    "s3_bucket": "shared-bucket",
    "s3_access_key": "AKIAEXAMPLE",
    "s3_secret_key": "секретный",
    "s3_prefix": "a_clients_project_2026/china_bot_backaps/",
}


def _settings(**over) -> Settings:
    return Settings(**{**BASE, **over})  # type: ignore[arg-type]


# --- имена ---------------------------------------------------------------------


def test_имя_копии_содержит_дату():
    assert backup_name(date(2026, 8, 24)) == "china_bot-2026-08-24.sql.gz"


@pytest.mark.parametrize(
    "имя",
    [
        # Чужие файлы в общем бакете. Ни один из них не должен опознаться как
        # наша копия, иначе уборка удалит чужое.
        "n8n-2026-08-24.sql.gz",
        "china_bot-2026-08-24.tar",
        "china_bot.sql.gz",
        "china_bot-2026-13-40.sql.gz",
        "",
    ],
)
def test_чужой_файл_не_считается_нашей_копией(имя):
    assert parse_day(имя) is None


def test_своя_копия_опознаётся():
    assert parse_day("china_bot-2026-08-24.sql.gz") == date(2026, 8, 24)


# --- срок хранения -------------------------------------------------------------


def test_последняя_неделя_остаётся_целиком():
    сегодня = date(2026, 8, 24)
    дни = [date(2026, 8, d) for d in range(10, 25)]
    оставить, удалить = keep_plan(дни, сегодня, keep_daily=7, keep_weekly=0)

    for d in range(18, 25):
        assert date(2026, 8, d) in оставить
    assert date(2026, 8, 17) in удалить


def test_воскресенья_остаются_в_глубину():
    сегодня = date(2026, 8, 24)  # понедельник
    # Полтора месяца ежедневных копий.
    дни = [date(2026, 7, d) for d in range(15, 32)] + [date(2026, 8, d) for d in range(1, 25)]
    оставить, удалить = keep_plan(дни, сегодня, keep_daily=7, keep_weekly=4)

    воскресенья = [d for d in дни if d.weekday() == 6]
    assert set(воскресенья[-4:]) <= оставить
    # Более старые воскресенья и все будни за пределами недели — под удаление.
    assert воскресенья[0] in удалить
    assert date(2026, 8, 5) in удалить
    # Десять копий вместо сорока одной: семь дней подряд плюс три воскресенья
    # в глубину — четвёртое, 23 августа, и так попало в последнюю неделю.
    assert len(оставить) == 10


def test_сегодняшняя_копия_не_удаляется_никогда():
    # Настройки выкручены в ноль — задача всё равно не должна снести то, что
    # только что положила.
    сегодня = date(2026, 8, 24)
    оставить, удалить = keep_plan([сегодня], сегодня, keep_daily=0, keep_weekly=0)
    assert оставить == {сегодня}
    assert not удалить


def test_копия_из_будущего_не_трогается():
    # Сбитые часы или ручной эксперимент. Удалять чужие странности — не наше дело.
    сегодня = date(2026, 8, 24)
    завтра = date(2026, 8, 25)
    _, удалить = keep_plan([завтра], сегодня, keep_daily=7, keep_weekly=4)
    assert not удалить


# --- разговор с хранилищем -----------------------------------------------------

СПИСОК_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>shared-bucket</Name>
  <IsTruncated>false</IsTruncated>
  <Contents>
    <Key>a_clients_project_2026/china_bot_backaps/</Key>
    <Size>0</Size>
    <LastModified>2026-08-20T00:30:00.000Z</LastModified>
  </Contents>
  <Contents>
    <Key>a_clients_project_2026/china_bot_backaps/china_bot-2026-08-23.sql.gz</Key>
    <Size>1048576</Size>
    <LastModified>2026-08-23T00:30:12.000Z</LastModified>
  </Contents>
  <Contents>
    <Key>a_clients_project_2026/china_bot_backaps/n8n-2026-08-23.sql.gz</Key>
    <Size>4096</Size>
    <LastModified>2026-08-23T01:00:00.000Z</LastModified>
  </Contents>
</ListBucketResult>
"""


class _Хранилище:
    """Фальшивый http-клиент: запоминает запросы и отвечает по сценарию."""

    def __init__(self, status: int = 200, body: str = "") -> None:
        self.status = status
        self.body = body
        self.запросы: list[httpx.Request] = []

    async def request(self, method, url, content=None, headers=None, timeout=None):
        request = httpx.Request(method, url, headers=headers, content=content)
        self.запросы.append(request)
        return httpx.Response(self.status, text=self.body, request=request)


@pytest.fixture
def хранилище(monkeypatch):
    клиент = _Хранилище(body=СПИСОК_XML)
    monkeypatch.setattr("app.core.providers.storage.s3.get_client", lambda: клиент)
    return клиент


async def test_запрос_подписан_и_адресован_бакету(хранилище):
    await S3Storage(_settings()).put("папка/файл.sql.gz", b"dump")

    запрос = хранилище.запросы[0]
    assert запрос.method == "PUT"
    # Адресация путём: бакет в пути, а не в имени хоста — у стороннего S3
    # сертификат выписан на общий хост.
    путь = urllib.parse.quote("папка/файл.sql.gz", safe="/")
    assert str(запрос.url) == f"https://s3.example.cloud/shared-bucket/{путь}"
    assert запрос.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "x-amz-content-sha256" in запрос.headers
    assert "x-amz-date" in запрос.headers


async def test_подпись_у_разных_тел_разная(хранилище):
    storage = S3Storage(_settings())
    await storage.put("файл", b"first")
    await storage.put("файл", b"second")
    первая, вторая = (з.headers["Authorization"] for з in хранилище.запросы)
    # Тело входит в подпись: иначе запрос можно было бы подменить по дороге.
    assert первая != вторая


async def test_список_отсеивает_папку(хранилище):
    объекты = await S3Storage(_settings()).list("a_clients_project_2026/china_bot_backaps/")
    имена = [o.name for o in объекты]
    # Сама папка приходит объектом нулевого размера — в списке файлов ей не место.
    assert имена == ["china_bot-2026-08-23.sql.gz", "n8n-2026-08-23.sql.gz"]


async def test_отказ_хранилища_опознаётся(monkeypatch):
    клиент = _Хранилище(status=403, body="<Error><Code>AccessDenied</Code></Error>")
    monkeypatch.setattr("app.core.providers.storage.s3.get_client", lambda: клиент)

    with pytest.raises(ProviderError) as сбой:
        await S3Storage(_settings()).put("файл", b"dump")
    assert сбой.value.provider == "s3"
    assert сбой.value.status_code == 403
    # Тело нужно целиком: «нет доступа» и «нет такого бакета» лечатся по-разному.
    assert "AccessDenied" in (сбой.value.body or "")


async def test_ненастроенное_хранилище_говорит_об_этом():
    storage = S3Storage(_settings(s3_access_key=None))
    assert storage.configured is False
    with pytest.raises(ProviderError):
        await storage.put("файл", b"dump")


# --- уборка на живом списке ----------------------------------------------------


class _Хранилка:
    """Хранилище в памяти: помнит, что у него просили удалить."""

    def __init__(self, имена: list[str]) -> None:
        self.объекты = [
            StoredObject(key=BASE["s3_prefix"] + n, size=1024, modified=datetime.now())
            for n in имена
        ]
        self.удалённые: list[str] = []

    async def list(self, prefix: str) -> list[StoredObject]:
        return list(self.объекты)

    async def delete(self, key: str) -> None:
        self.удалённые.append(key)
        self.объекты = [o for o in self.объекты if o.key != key]


async def test_уборка_не_трогает_чужие_файлы():
    хранилка = _Хранилка(
        [
            "china_bot-2026-06-01.sql.gz",  # старьё, под удаление
            "china_bot-2026-08-24.sql.gz",  # сегодняшняя
            "n8n-2026-06-01.sql.gz",  # чужая и старая — не наше дело
            "важное.zip",
        ]
    )
    удалено = await prune(хранилка, _settings(), date(2026, 8, 24))

    assert удалено == ["china_bot-2026-06-01.sql.gz"]
    assert хранилка.удалённые == [BASE["s3_prefix"] + "china_bot-2026-06-01.sql.gz"]


async def test_неудачное_удаление_не_роняет_уборку():
    class _Упрямое(_Хранилка):
        async def delete(self, key: str) -> None:
            raise RuntimeError("хранилище недоступно")

    # Копия уже уехала, и споткнуться на уборке — не повод считать её неудачной.
    удалено = await prune(_Упрямое(["china_bot-2026-06-01.sql.gz"]), _settings(), date(2026, 8, 24))
    assert удалено == []


# --- что видит админ -----------------------------------------------------------


def test_свежая_копия_не_считается_протухшей(monkeypatch):
    monkeypatch.setattr("app.core.services.backup.today_msk", lambda: date(2026, 8, 24))
    статус = BackupStatus(
        configured=True,
        last=StoredObject(key="k/china_bot-2026-08-24.sql.gz", size=10, modified=datetime.now()),
        count=7,
    )
    assert статус.stale is False


def test_старая_копия_протухла(monkeypatch):
    monkeypatch.setattr("app.core.services.backup.today_msk", lambda: date(2026, 8, 24))
    статус = BackupStatus(
        configured=True,
        last=StoredObject(key="k/china_bot-2026-08-20.sql.gz", size=10, modified=datetime.now()),
        count=7,
    )
    # Ночная задача молчит четвёртые сутки — на такую копию уже нельзя
    # рассчитывать, и админ обязан это увидеть.
    assert статус.stale is True


def test_пустое_хранилище_это_тоже_тревога():
    assert BackupStatus(configured=True, last=None, count=0).stale is True
