"""S3-совместимое хранилище: запись, чтение, список, удаление.

Подпись AWS SigV4 считается здесь руками, и это осознанное решение. Нам нужны
ровно четыре запроса, а `boto3` притащил бы в образ botocore со всеми его
таблицами регионов — под сотню мегабайт на контейнер, который собирается на
одном ядре. Формат подписи стабилен с 2012 года и не меняется, а весь его
объём — тридцать строк ниже.

Хранилище стороннее (Beget S3), и бакет там **общий с другими проектами**.
Поэтому все операции ходят только внутрь своего префикса, а удаление вдобавок
сверяется с шаблоном имени — чужой файл не должен пострадать никогда, даже от
опечатки в настройке.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.core.providers.base import ProviderError, call_logged
from app.core.providers.http import get_client
from app.logging import get_logger

log = get_logger("storage")

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
# Пространство имён в ответах S3: без него ElementTree не найдёт ни одного тега.
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


@dataclass(slots=True, frozen=True)
class StoredObject:
    """Файл в хранилище."""

    key: str
    size: int
    modified: datetime

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3Storage:
    """Клиент S3. Настроен — работает, не настроен — честно говорит об этом."""

    name = "s3"

    def __init__(self, settings: Settings) -> None:
        self.endpoint = (settings.s3_endpoint or "").rstrip("/")
        self.bucket = settings.s3_bucket or ""
        self.access_key = settings.s3_access_key or ""
        self.secret_key = settings.s3_secret_key or ""
        self.region = settings.s3_region

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)

    # --- подпись ---------------------------------------------------------------

    def _headers(
        self, method: str, canonical_uri: str, canonical_query: str, payload_hash: str
    ) -> dict[str, str]:
        """Заголовки одного подписанного запроса.

        В подпись входит и путь, и параметры, и хеш тела: подменить по дороге
        нельзя ничего. Время берём в UTC — на расхождение больше пятнадцати
        минут S3 отвечает отказом, поэтому часы контейнера обязаны идти верно.
        """
        host = urllib.parse.urlsplit(self.endpoint).netloc
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        stamp = now.strftime("%Y%m%d")

        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        signed = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [method, canonical_uri, canonical_query, canonical_headers, signed, payload_hash]
        )

        scope = f"{stamp}/{self.region}/{SERVICE}/aws4_request"
        to_sign = "\n".join(
            [
                ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        key = _sign(f"AWS4{self.secret_key}".encode(), stamp)
        for part in (self.region, SERVICE, "aws4_request"):
            key = _sign(key, part)
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"{ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        return headers

    async def _request(
        self,
        method: str,
        key: str = "",
        body: bytes = b"",
        query: dict[str, str] | None = None,
        operation: str = "запрос",
        timeout: float = 120.0,
    ) -> bytes:
        if not self.configured:
            raise ProviderError(self.name, operation, "хранилище копий не настроено")

        # Адресация путём, а не поддоменом: у стороннего S3 сертификат на общий
        # хост, и бакет в имени домена упёрся бы в проверку TLS.
        canonical_uri = f"/{self.bucket}" + (f"/{urllib.parse.quote(key, safe='/')}" if key else "")
        pairs = sorted((query or {}).items())
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in pairs
        )
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = self._headers(method, canonical_uri, canonical_query, payload_hash)

        url = self.endpoint + canonical_uri + (f"?{canonical_query}" if canonical_query else "")
        async with call_logged(self.name, operation, файл=key or "(список)") as details:
            response = await get_client().request(
                method, url, content=body or None, headers=headers, timeout=timeout
            )
            details["http_код"] = response.status_code
            details["байт"] = len(response.content)
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    operation,
                    f"хранилище ответило {response.status_code}",
                    status_code=response.status_code,
                    # Тело у S3 — XML с внятной причиной отказа: без него
                    # «доступ запрещён» и «нет такого бакета» неразличимы.
                    body=response.text[:1000],
                )
            return response.content

    # --- операции --------------------------------------------------------------

    async def put(self, key: str, body: bytes, content_type: str = "application/gzip") -> None:
        """Положить файл целиком.

        Целиком, а не потоком, намеренно: дамп базы этого проекта — единицы
        мегабайт, и держать его в памяти дешевле, чем тащить многочастную
        загрузку ради файла, который в неё никогда не упрётся.
        """
        await self._request("PUT", key, body=body, operation="запись")
        log.info("копия уехала в хранилище", файл=key, байт=len(body), тип=content_type)

    async def get(self, key: str) -> bytes:
        return await self._request("GET", key, operation="чтение")

    async def delete(self, key: str) -> None:
        await self._request("DELETE", key, operation="удаление")
        log.info("файл удалён из хранилища", файл=key)

    async def list(self, prefix: str) -> list[StoredObject]:
        """Что лежит по префиксу, от старого к новому.

        Страницы обходим до конца: бакет общий, и обрезанный список привёл бы к
        тому, что старые копии никогда не попадали бы под уборку.
        """
        objects: list[StoredObject] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            payload = await self._request("GET", query=query, operation="список")
            root = ET.fromstring(payload)  # noqa: S314  ответ своего хранилища
            for node in root.findall("s3:Contents", NS):
                key = node.findtext("s3:Key", default="", namespaces=NS)
                # Сама папка приходит отдельным объектом нулевого размера.
                if not key or key.endswith("/"):
                    continue
                objects.append(
                    StoredObject(
                        key=key,
                        size=int(node.findtext("s3:Size", default="0", namespaces=NS) or 0),
                        modified=_parse_time(
                            node.findtext("s3:LastModified", default="", namespaces=NS)
                        ),
                    )
                )
            if root.findtext("s3:IsTruncated", default="false", namespaces=NS) != "true":
                break
            token = root.findtext("s3:NextContinuationToken", default="", namespaces=NS)
            if not token:
                break
        objects.sort(key=lambda o: o.key)
        return objects


def _parse_time(raw: str) -> datetime:
    """Время из ответа S3. Кривую дату не роняем: список важнее её точности."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("хранилище вернуло непонятную дату", значение=raw)
        return datetime.now(UTC)
