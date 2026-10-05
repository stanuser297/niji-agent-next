"""Bounded importer for public GitHub repositories pinned to immutable revisions.

The trusted worker fetches archives only from GitHub's fixed HTTPS archive host.
Archives are validated in memory, then copied into a network-disabled sandbox.
This is not a general URL fetcher and does not support private repositories.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .cloud_runtime import _MAX_CLOUD_ARCHIVE_BYTES, _extract_cloud_archive, validate_cloud_payload

_GITHUB_OWNER = re.compile(r"^(?!-)[A-Za-z0-9-]{1,39}(?<!-)$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_GIT_COMMIT = re.compile(r"^[a-fA-F0-9]{40}$")


class RepositoryImportError(ValueError):
    """A public repository archive failed safe validation or bounded retrieval."""


def validate_repository_spec(value: Any) -> dict[str, str]:
    """Validate an exact public github.com repository URL and full commit SHA."""
    if not isinstance(value, dict) or set(value) != {"url", "revision"}:
        raise ValueError("repository requires only url and revision")
    url, revision = value.get("url"), value.get("revision")
    if not isinstance(url, str) or len(url) > 300 or not isinstance(revision, str):
        raise ValueError("repository URL and revision must be bounded text")
    parsed = urlsplit(url)
    if (parsed.scheme.lower() != "https" or parsed.hostname is None
            or parsed.hostname.lower() != "github.com" or parsed.port is not None
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("repository URL must be a public HTTPS github.com URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("repository URL must identify one GitHub owner and repository")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if (not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repo)
            or repo in {".", ".."} or ".." in repo):
        raise ValueError("repository name is invalid")
    if not _GIT_COMMIT.fullmatch(revision):
        raise ValueError("repository revision must be a full 40-character commit SHA")
    return {"url": f"https://github.com/{owner}/{repo}", "revision": revision.lower()}


def fetch_github_repository(
    repository: Any, *, transport: httpx.BaseTransport | None = None,
    check_cancelled: Callable[[], Any] | None = None,
) -> list[dict[str, str]]:
    """Download and validate a bounded public GitHub archive for a pinned commit.

    Redirects, credentials, arbitrary hosts, proxy environment settings, private
    repositories, and oversized/binary/unsafe archives are rejected.
    """
    spec = validate_repository_spec(repository)
    owner, repo = spec["url"].removeprefix("https://github.com/").split("/", 1)
    archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/{spec['revision']}"
    archive = bytearray()
    fetch_deadline = time.monotonic() + 30
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/zip", "User-Agent": "Niji-Agent-Repository-Importer"},
            transport=transport,
        ) as client:
            with client.stream("GET", archive_url) as response:
                if 300 <= response.status_code < 400:
                    raise RepositoryImportError("Repository archive redirects are not allowed")
                if response.status_code in (401, 403, 404):
                    raise RepositoryImportError(
                        "Repository must be public and the pinned revision must exist"
                    )
                if response.status_code != 200:
                    raise RepositoryImportError("GitHub repository archive could not be retrieved")
                length = response.headers.get("content-length")
                if length:
                    try:
                        if int(length) > _MAX_CLOUD_ARCHIVE_BYTES:
                            raise RepositoryImportError("Repository archive exceeds the 500 KB limit")
                    except ValueError as exc:
                        raise RepositoryImportError("GitHub returned an invalid archive size") from exc
                for chunk in response.iter_bytes():
                    if callable(check_cancelled):
                        check_cancelled()
                    if time.monotonic() > fetch_deadline:
                        raise RepositoryImportError("Repository archive download timed out")
                    archive.extend(chunk)
                    if len(archive) > _MAX_CLOUD_ARCHIVE_BYTES:
                        raise RepositoryImportError("Repository archive exceeds the 500 KB limit")
    except RepositoryImportError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise RepositoryImportError("GitHub repository archive could not be retrieved") from exc

    if not archive:
        raise RepositoryImportError("GitHub returned an empty repository archive")
    try:
        files = _extract_cloud_archive(base64.b64encode(archive).decode("ascii"))
        paths = [PurePosixPath(item["path"]) for item in files]
        roots = {path.parts[0] for path in paths if path.parts}
        strip_root = len(roots) == 1 and all(len(path.parts) > 1 for path in paths)
        normalized = [
            {"path": PurePosixPath(*path.parts[1:]).as_posix() if strip_root else path.as_posix(),
             "content": item["content"]}
            for path, item in zip(paths, files)
        ]
        _, safe_files = validate_cloud_payload({"prompt": "repository import", "files": normalized})
    except (ValueError, OSError) as exc:
        raise RepositoryImportError("Repository archive is unsafe or exceeds project limits") from exc
    return safe_files
