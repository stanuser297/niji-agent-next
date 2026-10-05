"""Additional practical Niji tools with bounded output and conservative defaults."""
from __future__ import annotations

import functools
import html
import http.client
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from ..safety import check_command, subprocess_environment
from .builtin import _truncate, edit_file
from .paths import workspace_cwd, workspace_path
from .subprocess_runner import run_process

MAX_OUTPUT = 12000
MAX_ARCHIVE_BYTES = 100_000_000


def _public_endpoint(url: str):
    """Validate a public HTTP endpoint and return its vetted, pinned socket addresses."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "only http:// and https:// URLs are allowed", ()
        if parsed.username or parsed.password:
            return "URLs containing embedded credentials are not allowed", ()
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return "local/private hosts are not allowed", ()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            return "URL host could not be safely resolved", ()
        # Pin the exact DNS results used for validation. The HTTP connection below
        # connects to these IPs directly instead of resolving the hostname again.
        for item in addresses:
            address = ipaddress.ip_address(item[4][0].split("%", 1)[0])
            if not address.is_global:
                return "local/private IP addresses are not allowed", ()
        return None, tuple(addresses)
    except (TypeError, ValueError, OSError, socket.gaierror):
        return "URL host could not be safely resolved", ()


def _public_url(url: str) -> str | None:
    """Validate URL syntax, DNS results and reject local/private destinations."""
    return _public_endpoint(url)[0]


def _connect_vetted(addresses, timeout, source_address):
    errors = []
    for family, socktype, proto, _canonname, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise OSError("no vetted public address was available")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *args, pinned_addresses, **kwargs):
        self._pinned_addresses = pinned_addresses
        super().__init__(host, *args, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise OSError("proxy tunnels are disabled for guarded public requests")
        self.sock = _connect_vetted(self._pinned_addresses, self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *args, pinned_addresses, **kwargs):
        self._pinned_addresses = pinned_addresses
        super().__init__(host, *args, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise OSError("proxy tunnels are disabled for guarded public requests")
        raw = _connect_vetted(self._pinned_addresses, self.timeout, self.source_address)
        try:
            # Keep the original hostname for certificate validation and TLS SNI
            # while the TCP socket is pinned to its already-vetted public IP.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    handler_order = 499

    def http_open(self, req):
        problem, addresses = _public_endpoint(req.full_url)
        if problem:
            raise urllib.error.URLError(problem)
        connection = functools.partial(_PinnedHTTPConnection, pinned_addresses=addresses)
        return self.do_open(connection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    handler_order = 499

    def https_open(self, req):
        problem, addresses = _public_endpoint(req.full_url)
        if problem:
            raise urllib.error.URLError(problem)
        connection = functools.partial(_PinnedHTTPSConnection, pinned_addresses=addresses)
        return self.do_open(connection, req, context=self._context,
                            check_hostname=self._check_hostname)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        reason = _public_url(target)
        if reason:
            raise urllib.error.HTTPError(target, code, f"blocked redirect: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _public_opener():
    # Do not let environment proxies hide the actual destination from validation.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _PublicRedirectHandler(),
                                       _PinnedHTTPHandler(), _PinnedHTTPSHandler())


def web_search(query: str, limit: int = 5) -> str:
    """Search public web using DuckDuckGo's HTML endpoint; returns source URLs."""
    query = str(query).strip()
    if not query:
        return "[error] query is required"
    limit = max(1, min(int(limit), 10))
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    problem = _public_url(url)
    if problem:
        return f"[error] {problem}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NijiAgent/2.8)"})
    try:
        with _public_opener().open(request, timeout=15) as response:
            page = response.read(800_000).decode("utf-8", errors="replace")
    except Exception as exc:
        return f"[error] web search failed: {exc}"
    # Extract search-result blocks without returning the page boilerplate.
    blocks = re.findall(r'(?is)<div[^>]+class=["\'][^"\']*result[^"\']*["\'][^>]*>(.*?)</div>\s*</div>', page)
    results = []
    for block in blocks:
        a = re.search(r'(?is)<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block)
        if not a:
            a = re.search(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*result__a[^"\']*["\'][^>]*>(.*?)</a>', block)
        if not a:
            continue
        title = re.sub(r"(?is)<[^>]+>", " ", a.group(2))
        title = re.sub(r"\s+", " ", html.unescape(title)).strip()
        target = html.unescape(a.group(1))
        parsed = urllib.parse.urlsplit(target)
        if "duckduckgo.com" in (parsed.hostname or "") and "uddg" in parsed.query:
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [target])[0]
        snippet_match = re.search(r'(?is)<(?:a|div)[^>]+class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</(?:a|div)>', block)
        snippet = (re.sub(r"(?is)<[^>]+>", " ", snippet_match.group(1)) if snippet_match else "")
        snippet = re.sub(r"\s+", " ", html.unescape(snippet)).strip()
        results.append(f"{len(results)+1}. {title}\n   {target}\n   {snippet}".rstrip())
        if len(results) >= limit:
            break
    return "\n".join(results) if results else "[no results parsed] Search page may be blocking automated requests; try web_fetch on a known source."


def file_search(query: str, path: str = ".", include: str = "*", ctx: dict = None) -> str:
    """Search filenames and text content for a literal, case-insensitive query."""
    base = workspace_path(path, ctx)
    if not base.is_dir():
        return f"[error] not a directory: {path}"
    term = str(query).strip()
    if not term:
        return "[error] query is required"
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
    hits, count = [], 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip]
        for filename in files:
            file_path = Path(root) / filename
            if not file_path.match(include):
                continue
            if term.casefold() in filename.casefold():
                hits.append(f"{file_path}: [filename match]")
            try:
                if file_path.stat().st_size > 1_000_000:
                    continue
                for line_no, line in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
                    if term.casefold() in line.casefold():
                        hits.append(f"{file_path}:{line_no}: {line.strip()[:220]}")
                        count += 1
                        if count >= 150 or len(hits) >= 180:
                            return "\n".join(hits) + "\n... [truncated]"
            except (OSError, UnicodeError):
                continue
    return "\n".join(hits) if hits else "[no matches]"


def apply_patch(path: str, old_text: str, new_text: str, ctx: dict = None) -> str:
    """Apply one exact, unique context replacement and record it for /undo."""
    if not old_text:
        return "[error] old_text must not be empty"
    return edit_file(path, old_text, new_text, ctx=ctx)


_GIT_READ = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "remote", "tag"}
_GIT_WRITE = {"add", "commit", "switch", "checkout", "restore", "merge", "rebase", "push", "pull", "reset", "stash", "tag"}

def git(args: list[str], cwd: str = ".", timeout: int = 30, ctx: dict = None) -> str:
    """Run a limited Git subcommand. Mutations should be used with --ask."""
    if not args or not isinstance(args, list):
        return "[error] pass args as a list, e.g. [\"status\", \"--short\"]"
    if any(not isinstance(x, str) or "\x00" in x for x in args):
        return "[error] invalid git argument"
    sub = args[0]
    if sub not in _GIT_READ | _GIT_WRITE:
        return f"[error] git subcommand not allowed: {sub}"
    if sub == "reset":
        return "[blocked] git reset is intentionally unavailable; use a deliberate, reversible commit instead"
    if sub == "push" and any(a in ("-f", "--force", "--force-with-lease") for a in args[1:]):
        return "[blocked] force-push is intentionally unavailable"
    if sub == "branch" and any(a in ("-d", "-D", "--delete") for a in args[1:]):
        return "[blocked] deleting branches is intentionally unavailable"
    if sub == "checkout" and "--" in args[1:]:
        return "[blocked] checkout of paths can discard edits; use a reviewed patch instead"
    if sub == "tag" and len(args) > 1 and args[1].startswith("-") and args[1] not in ("-l", "--list"):
        return "[error] tag deletion/creation flags are not permitted by this tool"
    timeout = max(1, min(int(timeout), 60))
    try:
        code, out, timed_out, cancelled = run_process(
            ["git", *args], cwd=workspace_cwd(cwd, ctx), timeout=timeout, env=subprocess_environment(),
            ctx=ctx, tool_name="git")
        if cancelled:
            return "[cancelled by user; inspect repository state before retrying]"
        if timed_out:
            return f"[error] git timed out after {timeout}s"
        out = out.strip()
        return f"[exit code {code}]\n{_truncate(out or '(no output)')}" if code else (_truncate(out) or "[ok] Git command finished")
    except FileNotFoundError:
        return "[error] git is not installed"
    except Exception as exc:
        return f"[error] {exc.__class__.__name__}: {exc}"


_TEST_COMMANDS = {
    "pytest": ["python", "-m", "pytest", "-q"],
    "unittest": ["python", "-m", "unittest", "discover", "-s", "tests"],
    "npm": ["npm", "test"],
    "go": ["go", "test", "./..."],
    "cargo": ["cargo", "test"],
}

def run_tests(kind: str = "auto", path: str = ".", timeout: int = 120, ctx: dict = None) -> str:
    """Run a known project test command; never accepts arbitrary shell text."""
    root = workspace_path(path, ctx).resolve()
    if not root.is_dir():
        return f"[error] not a directory: {path}"
    if kind == "auto":
        if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
            kind = "pytest" if _has_module("pytest") else "unittest"
        elif (root / "package.json").exists(): kind = "npm"
        elif (root / "go.mod").exists(): kind = "go"
        elif (root / "Cargo.toml").exists(): kind = "cargo"
        else: return "[error] no supported test project detected (Python, npm, Go, or Cargo)"
    if kind not in _TEST_COMMANDS:
        return "[error] kind must be auto, pytest, unittest, npm, go, or cargo"
    command = list(_TEST_COMMANDS[kind])
    if kind == "npm" and not (root / "package.json").exists():
        return "[error] package.json not found"
    if kind in ("go", "cargo") and not (root / ("go.mod" if kind == "go" else "Cargo.toml")).exists():
        return f"[error] {kind} project manifest not found"
    timeout = max(1, min(int(timeout), 120))
    try:
        code, output, timed_out, cancelled = run_process(
            command, cwd=root, timeout=timeout, env=subprocess_environment(),
            ctx=ctx, tool_name="run_tests")
        if cancelled:
            return "[cancelled by user; inspect test/build side effects before retrying]"
        if timed_out:
            return f"[error] tests timed out after {timeout}s\n{_truncate(output.strip())}"
        result = _truncate(output.strip())
        return f"[{kind} exit {code}]\n{result or '(no output)'}"
    except FileNotFoundError as exc:
        return f"[error] required executable not found: {exc.filename}"
    except Exception as exc:
        return f"[error] {exc.__class__.__name__}: {exc}"


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def package_manager(manager: str, action: str, packages: list[str] | None = None,
                    cwd: str = ".", timeout: int = 120, ctx: dict = None) -> str:
    """Inspect or install named packages using a small allowlisted set of managers."""
    packages = packages or []
    if any(p.startswith("-") or not re.fullmatch(r"[-A-Za-z0-9_.@/+:<>=!~*]+", p)
           for p in packages):
        return "[error] invalid package name; leading flags and shell syntax are not accepted"
    if action not in ("check", "install"):
        return "[error] action must be check or install"
    if action == "install" and not packages:
        return "[error] specify at least one package to install"
    if manager not in ("pip", "uv", "npm", "bun"):
        return "[error] manager must be pip, uv, npm, or bun"
    if manager == "pip":
        cmd = ["python", "-m", "pip", "show" if action == "check" else "install", *packages]
    elif manager == "uv":
        cmd = ["uv", "pip", "show" if action == "check" else "install", *packages]
    elif manager == "npm":
        cmd = ["npm", "ls", "--depth=0", *packages] if action == "check" else ["npm", "install", *packages]
    else:
        cmd = ["bun", "pm", "ls"] if action == "check" else ["bun", "add", *packages]
    timeout = max(1, min(int(timeout), 120))
    try:
        code, output, timed_out, cancelled = run_process(
            cmd, cwd=workspace_cwd(cwd, ctx), timeout=timeout, env=subprocess_environment(),
            ctx=ctx, tool_name="package_manager")
        if cancelled:
            return "[cancelled by user; inspect dependency changes before retrying]"
        if timed_out:
            return f"[error] package operation timed out after {timeout}s\n{_truncate(output.strip())}"
        return _truncate(f"[exit {code}]\n{output.strip() or '(no output)'}")
    except FileNotFoundError as exc:
        return f"[error] {manager} executable not found: {exc.filename}"
    except Exception as exc:
        return f"[error] {exc.__class__.__name__}: {exc}"


def database(path: str, query: str, max_rows: int = 100, ctx: dict = None) -> str:
    """Read-only SQLite query tool; accepts a single SELECT statement only."""
    sql = query.strip()
    if not re.match(r"(?is)^select\b", sql) or ";" in sql.rstrip(";"):
        return "[blocked] database tool permits a single SELECT query only"
    p = workspace_path(path, ctx).resolve()
    if not p.is_file():
        return f"[error] database file not found: {path}"
    max_rows = max(1, min(int(max_rows), 500))
    conn = None
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(p))}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(sql)
        columns = [item[0] for item in (cur.description or [])]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        payload = json.dumps([dict(zip(columns, row)) for row in rows], default=str, ensure_ascii=False)
        return payload + (f"\n[truncated at {max_rows} rows]" if truncated else "")
    except Exception as exc:
        return f"[error] SQLite query failed: {exc}"
    finally:
        if conn is not None:
            conn.close()


def http_request(url: str, method: str = "GET", max_chars: int = 8000) -> str:
    """Fetch a public URL with GET/HEAD only; no credentials or custom headers."""
    method = method.upper()
    if method not in ("GET", "HEAD"):
        return "[blocked] only GET and HEAD are supported"
    problem = _public_url(url)
    if problem:
        return f"[blocked] {problem}"
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "Niji-Agent/2.8"})
    try:
        with _public_opener().open(request, timeout=20) as response:
            final_problem = _public_url(response.geturl())
            if final_problem:
                return f"[blocked] redirected to unsafe destination: {final_problem}"
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            body = response.read(500_000) if method == "GET" else b""
        text = body.decode("utf-8", errors="replace")
        return f"HTTP {status} · {content_type}\n{_truncate(text[:max(500, min(int(max_chars), 12000))])}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return f"[error] request failed: {exc}"


MAX_ARCHIVE_MEMBERS = 2000
MAX_ARTIFACT_DOWNLOAD_BYTES = 10_000_000
_ARCHIVE_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ssh", ".aws", ".niji"}
_ARCHIVE_SKIP_NAMES = {".env", ".env.local", ".env.production", ".envrc", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
_ARCHIVE_SKIP_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def _create_zip_archive(path: str, output: str = "", ctx: dict = None) -> str:
    """Create a bounded ZIP from regular files inside the active workspace."""
    ctx = ctx or {}
    agent = ctx.get("agent")
    try:
        root = Path(getattr(agent, "workspace", Path.cwd()) or Path.cwd()).expanduser().resolve(strict=True)
        if not root.is_dir():
            return "[error] active workspace is not a directory"

        def checked_path(value):
            candidate = workspace_path(value, ctx)
            if ".." in candidate.parts:
                raise ValueError("parent-directory path segments are not allowed")
            candidate = Path(os.path.abspath(str(candidate)))
            try:
                relative = candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError("path must stay inside the active workspace") from exc
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("symlinks are not included in ZIP archives")
            return candidate

        source = checked_path(path)
        if source.is_symlink():
            return "[blocked] ZIP source cannot be a symlink"
        source = source.resolve(strict=True)
        source.relative_to(root)
        if not (source.is_file() or source.is_dir()):
            return "[error] ZIP source must be a regular file or directory"

        if output:
            output_path = checked_path(output)
        else:
            default_name = source.with_suffix(".zip").name if source.is_file() else (source.name or root.name) + ".zip"
            output_path = root / default_name
        if output_path.suffix.lower() != ".zip":
            output_path = output_path.with_suffix(".zip")
        try:
            output_path.relative_to(root)
        except ValueError:
            return "[blocked] ZIP output must stay inside the active workspace"
        if output_path == source:
            return "[error] ZIP output cannot overwrite its source"
        if output_path.exists() or output_path.is_symlink():
            return "[error] ZIP output already exists; choose a different output path"
        parent = output_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        checked_path(str(parent))
        if output_path.is_symlink():
            return "[blocked] ZIP output cannot be a symlink"

        files = []
        skipped = 0
        if source.is_file():
            lowered = source.name.lower()
            if (lowered in _ARCHIVE_SKIP_NAMES or lowered.startswith((".env.", "credentials.", "secrets."))
                    or source.suffix.lower() in _ARCHIVE_SKIP_SUFFIXES):
                return "[blocked] refusing to bundle a file that appears to contain credentials or a private key"
            files = [(source, source.name)]
        else:
            for directory, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
                base = Path(directory)
                retained_dirs = []
                for dirname in sorted(dirnames):
                    child = base / dirname
                    if dirname in _ARCHIVE_SKIP_DIRS or child.is_symlink():
                        skipped += 1
                    else:
                        retained_dirs.append(dirname)
                dirnames[:] = retained_dirs
                for filename in sorted(filenames):
                    child = base / filename
                    lowered = filename.lower()
                    if (lowered in _ARCHIVE_SKIP_NAMES or lowered.startswith(".env.")
                            or lowered.startswith(("credentials.", "secrets."))
                            or child.suffix.lower() in _ARCHIVE_SKIP_SUFFIXES
                            or child.is_symlink()):
                        skipped += 1
                        continue
                    if not child.is_file():
                        skipped += 1
                        continue
                    files.append((child, child.relative_to(source).as_posix()))
                    if len(files) > MAX_ARCHIVE_MEMBERS:
                        return f"[blocked] ZIP source exceeds the {MAX_ARCHIVE_MEMBERS}-file limit"

        if not files:
            return "[error] no regular files were found to archive"
        total_size = 0
        for file_path, _ in files:
            if file_path.is_symlink():
                return "[blocked] a ZIP source changed to a symlink during collection"
            try:
                total_size += file_path.stat().st_size
            except OSError:
                return "[error] a ZIP source file could not be read"
            if total_size > MAX_ARCHIVE_BYTES:
                return "[blocked] ZIP source exceeds the 100 MB uncompressed limit"

        fd, temporary_name = tempfile.mkstemp(prefix=".niji-zip-", suffix=".tmp", dir=str(parent))
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary_name, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=False) as zipped:
                for file_path, member_name in files:
                    if file_path.is_symlink() or not file_path.resolve(strict=True).is_relative_to(root):
                        return "[blocked] a ZIP source escaped the active workspace"
                    zipped.write(file_path, member_name)
            archive_size = Path(temporary_name).stat().st_size
            if archive_size > MAX_ARTIFACT_DOWNLOAD_BYTES:
                return "[blocked] ZIP exceeds the 10 MB local download limit; create smaller archives"
            try:
                os.link(temporary_name, output_path)
            except FileExistsError:
                return "[error] ZIP output already exists; choose a different output path"
            except OSError as exc:
                return f"[error] could not safely publish ZIP output ({type(exc).__name__})"
        finally:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass

        relative_output = output_path.resolve(strict=True).relative_to(root).as_posix()
        artifact_id = (agent.record_generated_artifact(output_path, "create_zip")
                       if hasattr(agent, "record_generated_artifact") else None)
        result = (f"[ok] Created ZIP archive {relative_output} ({archive_size:,} bytes, "
                  f"{len(files)} files" + (f", {skipped} excluded" if skipped else "") + ").")
        if artifact_id:
            result += "\nDownload link: [Download " + output_path.name + "](" + \
                "niji-artifact://" + urllib.parse.quote(relative_output, safe="/") + ")"
        return result
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return f"[error] ZIP creation failed: {str(exc)[:180]}"


def archive(action: str, path: str, destination: str = ".", limit: int = 200,
            output: str = "", ctx: dict = None) -> str:
    """Create a safe workspace ZIP, or list/extract ZIP/TAR archives."""
    if action == "create_zip":
        return _create_zip_archive(path, output, ctx)
    if action not in ("list", "extract"):
        return "[error] action must be list, extract, or create_zip"
    p = workspace_path(path, ctx)
    if not p.is_file():
        return f"[error] archive not found: {path}"
    limit = max(1, min(int(limit), 500))
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                infos = zf.infolist()
                if action == "list": return "\n".join(i.filename for i in infos[:limit]) or "[empty archive]"
                if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                    return "[blocked] archive expands beyond the 100 MB extraction limit"
                root = workspace_path(destination, ctx).resolve()
                for info in infos:
                    rel = PurePosixPath(info.filename)
                    mode = (info.external_attr >> 16) & 0o170000
                    target = (root / Path(*rel.parts)).resolve()
                    if rel.is_absolute() or ".." in rel.parts or not target.is_relative_to(root):
                        return f"[blocked] unsafe archive path: {info.filename}"
                    if mode not in (0, 0o100000, 0o040000):
                        return f"[blocked] links/special files are not extracted: {info.filename}"
                root.mkdir(parents=True, exist_ok=True)
                for info in infos[:limit]: zf.extract(info, root)
                return f"[ok] extracted {min(len(infos), limit)} of {len(infos)} entries to {root}"
        if tarfile.is_tarfile(p):
            with tarfile.open(p, "r:*") as tf:
                members = tf.getmembers()
                if action == "list": return "\n".join(m.name for m in members[:limit]) or "[empty archive]"
                if sum(m.size for m in members if m.isfile()) > MAX_ARCHIVE_BYTES:
                    return "[blocked] archive expands beyond the 100 MB extraction limit"
                root = workspace_path(destination, ctx).resolve()
                for member in members:
                    target = (root / member.name).resolve()
                    if Path(member.name).is_absolute() or ".." in Path(member.name).parts or not target.is_relative_to(root):
                        return f"[blocked] unsafe archive path: {member.name}"
                    if not (member.isfile() or member.isdir()):
                        return f"[blocked] links/special files are not extracted: {member.name}"
                root.mkdir(parents=True, exist_ok=True)
                for member in members[:limit]:
                    target = (root / member.name).resolve()
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = tf.extractfile(member)
                        if source is None:
                            continue
                        with source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(target, member.mode & 0o777)
                return f"[ok] extracted {min(len(members), limit)} of {len(members)} entries to {root}"
        return "[error] supported formats are ZIP and TAR"
    except Exception as exc:
        return f"[error] archive operation failed: {exc}"


_PROCESS_LOCK = threading.Lock()
_PROCESSES: dict[str, dict] = {}


def _capture_process_output(pipe, log_path: str):
    """Drain a child pipe without allowing its log to consume unbounded disk."""
    saved = 0
    cap = 1_000_000
    try:
        with open(log_path, "wb") as log:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                if saved < cap:
                    part = chunk[:cap - saved]
                    log.write(part)
                    saved += len(part)
            if saved >= cap:
                log.write(b"\n[log truncated at 1 MB]\n")
    except (OSError, ValueError):
        pass
    finally:
        try: pipe.close()
        except Exception: pass


def process_manager(action: str, process_id: str = "", command: str = "",
                    cwd: str = ".", timeout: int = 5, ctx: dict = None) -> str:
    """Start/inspect/stop session-owned long-running local processes."""
    if action == "start":
        if not command.strip(): return "[error] command is required"
        try: check_command(command)
        except PermissionError as exc: return f"[blocked by safety] {exc}"
        log = tempfile.NamedTemporaryFile(prefix="niji-process-", suffix=".log", delete=False)
        log_path = log.name
        log.close()
        try:
            proc = subprocess.Popen(command, shell=True, cwd=workspace_cwd(cwd, ctx), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, env=subprocess_environment())
            process_id = str(proc.pid)
            threading.Thread(target=_capture_process_output,
                             args=(proc.stdout, log_path), daemon=True).start()
            with _PROCESS_LOCK: _PROCESSES[process_id] = {"proc": proc, "log": log_path, "command": command[:160]}
            time.sleep(0.15)
            if proc.poll() is not None:
                return f"[process {process_id} exited {proc.returncode}]\n{process_logs(process_id)}"
            return f"[started] process_id={process_id}; use process_manager(action='status'/'logs'/'stop', process_id='{process_id}')"
        except Exception as exc:
            try: log.close()
            except Exception: pass
            return f"[error] could not start process: {exc}"
    with _PROCESS_LOCK: entry = _PROCESSES.get(str(process_id))
    if not entry: return "[error] process not found in this Niji session"
    proc = entry["proc"]
    if action == "status": return f"process {process_id}: {'running' if proc.poll() is None else f'exited {proc.returncode}'} · {entry['command']}"
    if action == "logs": return process_logs(process_id)
    if action == "stop":
        if proc.poll() is not None: return f"[already exited] {proc.returncode}"
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=max(1, min(int(timeout), 15)))
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except OSError: pass
        except OSError as exc: return f"[error] could not stop process: {exc}"
        return f"[stopped] process {process_id}"
    return "[error] action must be start, status, logs, or stop"


def process_logs(process_id: str) -> str:
    with _PROCESS_LOCK: entry = _PROCESSES.get(str(process_id))
    if not entry: return "[error] process not found in this Niji session"
    try:
        with open(entry["log"], "rb") as f:
            f.seek(max(0, os.path.getsize(entry["log"]) - 8000))
            return f.read().decode("utf-8", errors="replace") or "[no output yet]"
    except OSError as exc: return f"[error] could not read logs: {exc}"


def browser(url: str, actions: list | None = None, wait_ms: int = 0) -> str:
    """Optional Playwright browser session: navigate, click/fill/press, return text."""
    problem = _public_url(url)
    if problem: return f"[blocked] {problem}"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "[unavailable] Browser support needs optional Playwright: pip install 'niji-agent[browser]' then install Chromium with `playwright install chromium`. On Termux, configure a trusted browser MCP connector instead."
    actions = actions or []
    if len(actions) > 12: return "[error] at most 12 browser actions per call"
    try:
        with sync_playwright() as p:
            browser_instance = p.chromium.launch(headless=True)
            try:
                context = browser_instance.new_context()
                def guard_route(route):
                    reason = _public_url(route.request.url)
                    if reason:
                        route.abort()
                    else:
                        route.continue_()
                context.route("**/*", guard_route)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                for action in actions:
                    kind = action.get("type")
                    selector = str(action.get("selector", ""))[:500]
                    if kind == "click": page.locator(selector).click(timeout=5000)
                    elif kind == "fill": page.locator(selector).fill(str(action.get("value", ""))[:4000], timeout=5000)
                    elif kind == "press": page.locator(selector).press(str(action.get("value", "Enter"))[:30], timeout=5000)
                    elif kind == "wait": page.wait_for_timeout(max(0, min(int(action.get("value", 500)), 3000)))
                    else: return f"[error] unsupported browser action: {kind}"
                page.wait_for_timeout(max(0, min(int(wait_ms), 3000)))
                title = page.title()
                text = page.locator("body").inner_text(timeout=5000)[:MAX_OUTPUT]
                return f"{title}\nURL: {page.url}\n\n{text}"
            finally:
                browser_instance.close()
    except Exception as exc:
        return f"[error] browser session failed: {exc}"
