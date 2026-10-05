import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from niji.tools import CORE_SCHEMAS, HANDLERS, dispatch
from niji.tools.advanced import (
    _PublicRedirectHandler, _public_url, apply_patch, archive, database,
    file_search, git, http_request, package_manager, run_tests, web_search,
)


class AdvancedToolCatalogTests(unittest.TestCase):
    def test_all_suggested_tool_areas_are_registered(self):
        required = {
            "web_search", "browser", "git", "run_tests", "file_search",
            "apply_patch", "package_manager", "memory_read", "todo_read",
            "read_image", "database", "http_request", "archive", "process_manager",
        }
        self.assertTrue(required.issubset(HANDLERS))
        schema_names = {s["function"]["name"] for s in CORE_SCHEMAS}
        self.assertTrue(required.issubset(schema_names))
        self.assertEqual(len(schema_names), len(CORE_SCHEMAS))


class FileAndDatabaseToolTests(unittest.TestCase):
    def test_file_search_matches_name_and_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Needle.py").write_text("hello world")
            (root / "other.py").write_text("some NEEDLE inside")
            result = file_search("needle", d, "*.py")
            self.assertIn("Needle.py: [filename match]", result)
            self.assertIn("other.py:1:", result)

    def test_apply_patch_records_undo_checkpoint(self):
        class FakeAgent:
            def __init__(self): self.changes = []
            def record_file_change(self, path, before, after, operation):
                self.changes.append((path, before, after, operation))
                return True
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "file.txt"
            p.write_text("before phrase")
            agent = FakeAgent()
            result = apply_patch(str(p), "before", "after", {"agent": agent})
            self.assertIn("undo checkpoint", result)
            self.assertEqual(p.read_text(), "after phrase")
            self.assertEqual(agent.changes[0][3], "edit_file")

    def test_database_allows_select_and_blocks_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "db.sqlite"
            with sqlite3.connect(p) as conn:
                conn.execute("create table sample (value text)")
                conn.execute("insert into sample values ('ok')")
            self.assertIn('"value": "ok"', database(str(p), "SELECT value FROM sample"))
            self.assertIn("blocked", database(str(p), "DELETE FROM sample"))
            with sqlite3.connect(p) as conn:
                self.assertEqual(conn.execute("select count(*) from sample").fetchone()[0], 1)

    def test_archive_rejects_zip_path_traversal_before_extracting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as zf:
                zf.writestr("../escape.txt", "no")
            dest = root / "out"
            result = archive("extract", str(archive_path), str(dest))
            self.assertIn("unsafe archive path", result)
            self.assertFalse((root / "escape.txt").exists())

    def test_archive_create_zip_registers_only_safe_workspace_files(self):
        class FakeAgent:
            def __init__(self, workspace):
                self.workspace = workspace
                self.generated_artifacts = []
            def record_generated_artifact(self, path, operation):
                artifact_id = "a" * 16
                self.generated_artifacts.append({"id": artifact_id, "path": str(path), "operation": operation})
                return artifact_id
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d).resolve()
            source = root / "package"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "readme.txt").write_text("hello")
            (source / "app.py").write_text("print('safe')")
            (source / ".env").write_text("API_KEY=do-not-ship")
            (source / "node_modules").mkdir()
            (source / "node_modules" / "large.js").write_text("ignored")
            outside_secret = Path(outside) / "secret.txt"
            outside_secret.write_text("outside")
            (source / "external-link.txt").symlink_to(outside_secret)
            agent = FakeAgent(root)
            result = archive("create_zip", "package", output="exports/package.zip", ctx={"agent": agent})
            self.assertIn("[ok] Created ZIP archive exports/package.zip", result)
            self.assertIn("niji-artifact://exports/package.zip", result)
            self.assertEqual(agent.generated_artifacts[0]["operation"], "create_zip")
            with zipfile.ZipFile(root / "exports" / "package.zip") as zf:
                self.assertEqual(set(zf.namelist()), {"app.py", "nested/readme.txt"})
                self.assertNotIn(b"do-not-ship", zf.read("app.py"))

    def test_archive_create_zip_blocks_outside_existing_and_secret_targets(self):
        class FakeAgent:
            def __init__(self, workspace): self.workspace = workspace
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d).resolve()
            (root / "source.txt").write_text("safe")
            (root / "existing.zip").write_text("keep")
            (root / ".env").write_text("secret")
            agent = FakeAgent(root)
            ctx = {"agent": agent}
            self.assertIn("inside the active workspace", archive("create_zip", str(Path(outside)), ctx=ctx))
            self.assertIn("already exists", archive("create_zip", "source.txt", output="existing.zip", ctx=ctx))
            self.assertIn("credentials or a private key", archive("create_zip", ".env", ctx=ctx))
            self.assertEqual((root / "existing.zip").read_text(), "keep")

    def test_archive_create_zip_enforces_member_and_download_limits(self):
        class FakeAgent:
            def __init__(self, workspace): self.workspace = workspace
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "payload.bin").write_bytes(b"payload")
            ctx = {"agent": FakeAgent(root)}
            with patch("niji.tools.advanced.MAX_ARCHIVE_BYTES", 2):
                self.assertIn("100 MB uncompressed limit", archive("create_zip", "payload.bin", ctx=ctx))
            with patch("niji.tools.advanced.MAX_ARTIFACT_DOWNLOAD_BYTES", 1):
                self.assertIn("10 MB local download limit", archive("create_zip", "payload.bin", output="too-big.zip", ctx=ctx))
            self.assertFalse((root / "too-big.zip").exists())

    def test_archive_schema_exposes_approved_zip_creation(self):
        schema = next(s["function"] for s in CORE_SCHEMAS if s["function"]["name"] == "archive")
        self.assertIn("create_zip", schema["parameters"]["properties"]["action"]["enum"])
        self.assertIn("output", schema["parameters"]["properties"])


class NetworkAndCommandGuardTests(unittest.TestCase):
    def test_url_guard_blocks_loopback_and_embedded_credentials(self):
        self.assertIn("local/private", _public_url("http://127.0.0.1/"))
        self.assertIn("credentials", _public_url("https://user:pass@example.com/"))
        self.assertIsNone(_public_url("https://example.com/"))

    def test_http_tool_blocks_post_and_private_address(self):
        self.assertIn("only GET and HEAD", http_request("https://example.com", "POST"))
        self.assertIn("private", http_request("http://127.0.0.1/"))

    def test_git_tool_only_allows_known_subcommands(self):
        self.assertIn("not allowed", git(["clean", "-fd"]))

    def test_web_search_extracts_titles_snippets_and_urls(self):
        import niji.tools.advanced as advanced
        page = (b'<div class="result results_links"><div class="links_main">'
                b'<h2><a class="result__a" href="https://example.org/page">Example &amp; title</a></h2>'
                b'<a class="result__snippet">Useful snippet</a></div></div>')
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return page
        class FakeOpener:
            def open(self, request, timeout): return FakeResponse()
        with patch.object(advanced, "_public_url", return_value=None):
            with patch.object(advanced, "_public_opener", return_value=FakeOpener()):
                result = web_search("sample")
        self.assertIn("Example & title", result)
        self.assertIn("https://example.org/page", result)
        self.assertIn("Useful snippet", result)

    def test_redirect_handler_rejects_private_redirect(self):
        import urllib.request
        handler = _PublicRedirectHandler()
        request = urllib.request.Request("https://public.example/")
        with patch("niji.tools.advanced._public_url", return_value="local/private host"):
            with self.assertRaises(Exception):
                handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/")

    def test_package_manager_rejects_option_injection(self):
        self.assertIn("invalid package", package_manager("pip", "install", ["--index-url=x"]))
        self.assertIn("invalid package", package_manager("pip", "install", ["safe;evil"]))

    def test_test_runner_rejects_arbitrary_command_kind(self):
        self.assertIn("kind must be", run_tests("sh -c evil"))


if __name__ == "__main__":
    unittest.main()
