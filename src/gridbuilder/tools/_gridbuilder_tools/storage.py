# SPDX-License-Identifier: Apache-2.0
"""Storage backend for grid-builder outputs — local paths or ``s3://``.

Domain-local by design: the tools contract forbids `_<pkg>_tools/` depending on
the Facetwork runtime, because a tool has to run from a terminal with no cluster
present. So this mirrors the runtime's storage abstraction rather than importing
it, the same way `_noaa_tools.storage` does.

Why it exists at all: on a fleet, `FW_DATA_ROOT` is `s3://afl-cache` and the
runners are containers. Writing outputs with `pathlib` puts them on a container
filesystem that disappears with the container — the run succeeds and the data is
gone. A `Path("s3://afl-cache/x")` is worse: it silently creates a directory
literally named `s3:`.

`earth_osm` needs a real filesystem to write into, so extraction always stages
locally and the results are *finalized* to the backend afterwards. That ordering
is also what makes a partial write impossible to mistake for a finished one.
"""

from __future__ import annotations

import abc
import json
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class Storage(abc.ABC):
    """The operations this domain needs. Deliberately small."""

    @abc.abstractmethod
    def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    def size(self, path: str) -> int: ...

    @abc.abstractmethod
    def mkdir_p(self, path: str) -> None: ...

    @abc.abstractmethod
    def read_text(self, path: str) -> str: ...

    @abc.abstractmethod
    def write_text(self, path: str, text: str) -> None: ...

    @abc.abstractmethod
    def finalize_from_local(self, local_path: str, dest: str) -> None:
        """Move a staged local file to its final home."""

    @staticmethod
    def join(*parts: str) -> str:
        head, *rest = [p for p in parts if p]
        return "/".join([head.rstrip("/"), *[p.strip("/") for p in rest]]) if rest else head


class LocalStorage(Storage):
    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def size(self, path: str) -> int:
        return Path(path).stat().st_size

    def mkdir_p(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: str, text: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def finalize_from_local(self, local_path: str, dest: str) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_path), str(dest))


class S3Storage(Storage):
    """S3 / MinIO. Configured from the same environment the fleet already sets.

    Uploads use `upload_file`, i.e. the managed transfer — a continental PBF's
    GeoJSON can be hundreds of MB, and buffering that in memory to `put_object`
    is how a runner gets OOM-killed mid-fan-out.
    """

    def __init__(self) -> None:
        # ⚠️ The runtime's single S3 client construction — one credential chain
        # and one MinIO config (s3v4 + path-style) for the whole codebase. This
        # module's own client omitted that config and worked only because boto3
        # happens to choose path-style for a custom endpoint_url.
        try:
            from facetwork.runtime.storage import s3_client
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "s3:// output needs boto3 — install the package's [s3] extra"
            ) from exc
        self._client = s3_client()

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        parsed = urlparse(path)
        return parsed.netloc, parsed.path.lstrip("/")

    def exists(self, path: str) -> bool:
        bucket, key = self._split(path)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            # An auth failure or a throttle is NOT "absent". Masking it would
            # make a cache check report "missing" and silently re-extract, or
            # worse, report "present" for data nobody can read.
            raise

    def size(self, path: str) -> int:
        bucket, key = self._split(path)
        return int(self._client.head_object(Bucket=bucket, Key=key)["ContentLength"])

    def mkdir_p(self, path: str) -> None:
        return None  # object stores have no directories

    def read_text(self, path: str) -> str:
        bucket, key = self._split(path)
        body = self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return body.decode("utf-8", errors="replace")

    def write_text(self, path: str, text: str) -> None:
        bucket, key = self._split(path)
        self._client.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))

    def finalize_from_local(self, local_path: str, dest: str) -> None:
        bucket, key = self._split(dest)
        self._client.upload_file(str(local_path), bucket, key)
        Path(local_path).unlink(missing_ok=True)


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def get_storage(path: str) -> Storage:
    """Backend for *path*, chosen by URI scheme."""
    scheme = urlparse(path).scheme
    if scheme == "s3":
        return S3Storage()
    if scheme in ("", "file"):
        return LocalStorage()
    raise ValueError(
        f"unsupported storage scheme {scheme!r} in {path!r} — this domain writes "
        "local paths or s3:// (hdfs:// is not implemented here)"
    )


def read_json(path: str) -> dict | None:
    """Parse a JSON document, or None when absent/unreadable."""
    storage = get_storage(path)
    if not storage.exists(path):
        return None
    try:
        data = json.loads(storage.read_text(path))
    except (OSError, ValueError):
        logger.warning("unreadable JSON at %s — treating as absent", path)
        return None
    return data if isinstance(data, dict) else None
