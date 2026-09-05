from __future__ import annotations

import io
from typing import Any
from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError


class MinioUnavailable(RuntimeError):
    def __init__(self, message: str, category: str = "minio_failure"):
        self.category = category
        super().__init__(message)


class MinioObjectNotFoundError(LookupError):
    pass


def create_client(endpoint: str, access_key: str | None, secret_key: str | None, secure: bool = False) -> Minio:
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


def test_connection(client: Any) -> tuple[bool, str]:
    try:
        client.list_buckets()
        return True, "Connected"
    except MaxRetryError:
        return False, "MinIO connection was refused or the host is unreachable. Verify MINIO_ENDPOINT and that the MinIO server is running."
    except S3Error as exc:
        return False, f"MinIO rejected the request ({exc.code})."
    except Exception as exc:
        return False, f"MinIO health check failed ({type(exc).__name__})."


def ensure_bucket(client: Any, bucket: str) -> None:
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception as exc:
        raise MinioUnavailable(f"MinIO bucket '{bucket}' could not be created or verified.") from exc


def put_object(client: Any, bucket: str, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    ensure_bucket(client, bucket)
    try:
        client.put_object(bucket, object_key, io.BytesIO(data), length=len(data), content_type=content_type)
        return object_key
    except Exception as exc:
        raise MinioUnavailable(f"Object '{object_key}' could not be stored in bucket '{bucket}'.") from exc


def get_object(client: Any, bucket: str, object_key: str) -> bytes:
    response = None
    try:
        response = client.get_object(bucket, object_key)
        return response.read()
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            raise MinioObjectNotFoundError(f"Object '{object_key}' was not found in bucket '{bucket}'.") from exc
        raise MinioUnavailable(f"Object '{object_key}' could not be retrieved from bucket '{bucket}'.") from exc
    except Exception as exc:
        raise MinioUnavailable(f"Object '{object_key}' could not be retrieved from bucket '{bucket}'.") from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()


def delete_object(client: Any, bucket: str, object_key: str) -> None:
    try:
        client.remove_object(bucket, object_key)
    except Exception as exc:
        raise MinioUnavailable(f"Object '{object_key}' could not be deleted from bucket '{bucket}'.") from exc
