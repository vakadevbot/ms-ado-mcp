"""Controlled streaming downloads and inspection; never extract or execute content."""

import codecs
import hashlib
import os
import re
import stat
import struct
import uuid
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from .read_api import api_version, number


class Downloads:
    def __init__(self, api):
        self.api = api
        self.files = {}

    async def fetch(self, project, area, path, params, suffix=".zip"):
        project = self.api.project(project)
        settings = self.api.settings
        if settings.download_dir is None:
            raise ValueError("Downloads disabled; configure download_dir locally.")
        root = settings.download_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        handle = uuid.uuid4().hex
        target = root / (handle + suffix)
        temporary = root / (handle + ".part")
        url = self.api.url(project, area, path)
        params = {**params, "api-version": api_version(area, path)}
        digest, size = hashlib.sha256(), 0
        try:
            # Only the constructed Azure endpoint receives PAT. A redirect never does,
            # including a redirect back to the same host. Cookies are never forwarded.
            request = self.api.client.http.build_request(
                "GET",
                url,
                params=params,
                headers={
                    "Accept-Encoding": "identity",
                    "Accept": "application/zip" if suffix == ".zip" else "application/octet-stream",
                },
            )
            response = await self.api.client.http.send(request, stream=True)
            for _ in range(4):
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location", "")
                await response.aclose()
                try:
                    destination = urlsplit(urljoin(str(response.url), location))
                    port = destination.port
                except ValueError:
                    raise ValueError("Invalid download redirect blocked.") from None
                if (
                    destination.scheme != "https"
                    or destination.username
                    or destination.password
                    or destination.fragment
                    or port not in {None, 443}
                    or destination.hostname not in settings.download_hosts
                ):
                    raise ValueError(
                        "Download redirect blocked; configure its exact trusted host locally."
                    )
                request = httpx.Request(
                    "GET",
                    destination.geturl(),
                    headers={"Accept": "application/octet-stream", "Accept-Encoding": "identity"},
                )
                response = await self.api.client.http.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
            try:
                self.api.check(response, area, "download")
                length = response.headers.get("content-length")
                try:
                    declared_size = int(length) if length else None
                except ValueError:
                    raise ValueError("Invalid download size header.") from None
                if declared_size is not None and declared_size > settings.max_download_bytes:
                    raise ValueError("Download exceeds configured size limit.")
                with temporary.open("xb") as stream:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        size += len(chunk)
                        if size > settings.max_download_bytes:
                            raise ValueError("Download exceeds configured size limit.")
                        digest.update(chunk)
                        stream.write(chunk)
                if (
                    declared_size is not None
                    and not response.headers.get("content-encoding")
                    and size != declared_size
                ):
                    raise ValueError("Incomplete download; size differs from Content-Length.")
                if suffix == ".zip":
                    with self.archive(temporary):
                        pass
                os.replace(temporary, target)
            finally:
                await response.aclose()
        except (httpx.RequestError, httpx.InvalidURL):
            raise self.api.fail(area, "download", reason="Network/TLS/timeout failure") from None
        except OSError:
            raise self.api.fail(area, "download", reason="Local storage unavailable") from None
        finally:
            temporary.unlink(missing_ok=True)
        self.files[handle] = (project, target)
        return {
            "download_id": handle,
            "local_path": str(target),
            "size": size,
            "sha256": digest.hexdigest(),
            "untrusted_data": True,
        }

    def local(self, download_id, project):
        project = self.api.project(project)
        if not re.fullmatch(r"[0-9a-f]{32}", download_id) or download_id not in self.files:
            raise ValueError("Unknown download ID; use a download from this server session.")
        owner, path = self.files[download_id]
        if (
            owner != project
            or path.is_symlink()
            or path.resolve().parent != self.api.settings.download_dir.resolve()
        ):
            raise ValueError("Download is outside the permitted scope.")
        return path

    def archive(self, path):
        """Bound central-directory memory BEFORE ZipFile parses attacker-controlled entries."""
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 65557))
                tail = stream.read(65557)
            pos = tail.rfind(b"PK\x05\x06")
            if pos < 0 or len(tail) - pos < 22:
                raise ValueError("Invalid ZIP archive.")
            _, disk, start_disk, entries_disk, entries, central_size, _, comment = struct.unpack(
                "<4s4H2IH", tail[pos : pos + 22]
            )
            if (
                disk
                or start_disk
                or entries_disk != entries
                or entries > 10000
                or central_size > 4 * 1024 * 1024
                or pos + 22 + comment != len(tail)
            ):
                raise ValueError("ZIP64, split or oversized ZIP directories are unsupported.")
            archive = zipfile.ZipFile(path)
            try:
                if len(archive.infolist()) != entries:
                    raise ValueError("Inconsistent ZIP directory entry count.")
                total = 0
                names = set()
                for entry in archive.infolist():
                    name = entry.filename
                    if (
                        name in names
                        or "\\" in name
                        or ":" in name
                        or "\x00" in name
                        or PurePosixPath(name).is_absolute()
                        or ".." in PurePosixPath(name).parts
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.flag_bits & 1
                    ):
                        raise ValueError("Unsafe ZIP member or duplicate name.")
                    names.add(name)
                    total += entry.file_size
                    if (
                        total > 1024 * 1024 * 1024
                        or entry.file_size > 256 * 1024 * 1024
                        or entry.file_size > max(entry.compress_size, 1) * 200
                    ):
                        raise ValueError("ZIP expansion limit exceeded.")
                return archive
            except Exception:
                archive.close()
                raise
        except (zipfile.BadZipFile, OSError, struct.error):
            raise ValueError("Invalid or unavailable ZIP archive.") from None

    async def list_download_archive(
        self,
        download_id: str,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """List ZIP members without extraction. Only session-owned downloads can be read."""
        path = self.local(download_id, project)
        with self.archive(path) as archive:
            items = [
                {
                    "path": i.filename,
                    "size": i.file_size,
                    "compressed_size": i.compress_size,
                    "is_directory": i.is_dir(),
                }
                for i in archive.infolist()
            ]
        return self.api.slice(
            items, self.api.binding(download_id, items), limit, continuation_token
        )

    async def get_download_archive_file(
        self,
        download_id: str,
        member: str,
        project: str | None = None,
        start_line: int = 0,
        line_count: int = 200,
        max_bytes: int = 65536,
    ) -> dict:
        """Read UTF-8 ZIP member up to a byte cap. Large/binary members are metadata only."""
        number(start_line, 1000000, 0)
        number(line_count, 1000)
        number(max_bytes, 262144)
        path = self.local(download_id, project)
        try:
            with self.archive(path) as archive:
                entry = archive.getinfo(member)
                if entry.file_size > max_bytes:
                    return {
                        "size": entry.file_size,
                        "truncated": True,
                        "text": None,
                        "reason": "Member exceeds max_bytes; inspect locally",
                    }
                with archive.open(entry) as stream:
                    raw = stream.read(max_bytes + 1)
        except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise ValueError("ZIP member missing, corrupt or unsupported.") from None
        return text_content(raw, start_line, line_count, max_bytes)


def text_content(raw, start_line, line_count, max_bytes, truncated=False):
    number(start_line, 1000000, 0)
    number(line_count, 1000)
    number(max_bytes, 262144)
    lfs = raw.startswith(b"version https://git-lfs.github.com/spec/v1")
    try:
        if b"\x00" in raw:
            raise UnicodeError
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(raw, final=not truncated)
    except UnicodeError:
        return {
            "binary": True,
            "text": None,
            "lfs_pointer": lfs,
            "truncated": truncated,
            "reason": "Binary/non-UTF-8 content; use download",
            "untrusted_data": True,
        }
    lines = text.splitlines(keepends=True)
    selected = lines[start_line : start_line + line_count]
    end = start_line + len(selected)
    return {
        "text": "".join(selected),
        "binary": False,
        "lfs_pointer": lfs,
        "truncated": truncated or end < len(lines),
        "byte_limit_reached": truncated,
        "next_start_line": end if end < len(lines) else None,
        "untrusted_data": True,
    }


DOWNLOAD_TOOLS = ("list_download_archive", "get_download_archive_file")
