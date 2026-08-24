"""Хранилище файлов на стороне: копии базы уезжают туда, а не лежат рядом с ней."""

from app.core.providers.storage.s3 import S3Storage, StoredObject

__all__ = ["S3Storage", "StoredObject"]
