import io
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from niji.setup_wizard import run_setup
from niji.tools import CORE_SCHEMAS, HANDLERS, READ_ONLY_SUBAGENT_TOOLS
from niji.tools.advanced import (
    _PinnedHTTPConnection, _PinnedHTTPHandler, _PublicRedirectHandler, _public_url,
)
from niji.tools.builtin import web_fetch
from niji.tools.document import read_document


class PublicFetchSafetyTests(unittest.TestCase):
    def test_public_url_blocks_hostname_that_resolves_to_private_ip(self):
        private_dns = [(2, 1, 6, "", ("10.1.2.3", 80))]
        with patch("niji.tools.advanced.socket.getaddrinfo", return_value=private_dns):
            self.assertIn("private", _public_url("http://public-looking.example/"))

    def test_fetch_refuses_private_dns_before_opening_a_connection(self):
        private_dns = [(2, 1, 6, "", ("192.168.1.8", 80))]
        with patch("niji.tools.advanced.socket.getaddrinfo", return_value=private_dns):
            with patch("niji.tools.advanced._public_opener") as opener:
                result = web_fetch("http://attacker-controlled.example/")
        self.assertIn("private", result)
        opener.assert_not_called()

    def test_http_handler_passes_validated_ip_to_connection_factory(self):
        pinned = [(2, 1, 6, "", ("93.184.216.34", 80))]
        request = urllib.request.Request("http://public.example/")
        handler = _PinnedHTTPHandler()
        with patch("niji.tools.advanced._public_endpoint", return_value=(None, tuple(pinned))):
            with patch.object(handler, "do_open", return_value="opened") as do_open:
                self.assertEqual(handler.http_open(request), "opened")
        factory = do_open.call_args.args[0]
        connection = factory("public.example", timeout=5)
        self.assertEqual(connection._pinned_addresses, tuple(pinned))

    def test_pinned_connection_uses_only_the_vetted_socket_address(self):
        import socket
        address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                   "", ("93.184.216.34", 80))
        fake_socket = MagicMock()
        connection = _PinnedHTTPConnection("public.example", timeout=5,
                                           pinned_addresses=(address,))
        with patch("niji.tools.advanced.socket.socket", return_value=fake_socket):
            connection.connect()
        fake_socket.connect.assert_called_once_with(address[4])
        self.assertIs(connection.sock, fake_socket)

    def test_redirect_handler_rejects_private_target(self):
        handler = _PublicRedirectHandler()
        request = urllib.request.Request("https://public.example/")
        with patch("niji.tools.advanced._public_url", return_value="local/private host"):
            with self.assertRaises(Exception):
                handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/")


class CustomProviderFailureTests(unittest.TestCase):
    def test_failed_custom_provider_test_shows_guidance_not_unbound_variable_crash(self):
        from rich.console import Console

        cfg = {"provider": "custom", "custom_providers": {}}
        custom = {"base_url": "https://api.example.test/v1", "model": "test-model",
                  "api_key": "test-key"}
        provider_cfg = {"provider": "example", "base_url": custom["base_url"],
                        "model": custom["model"], "api_key": custom["api_key"]}
        with patch("niji.config.load_config", return_value=cfg), \
             patch("niji.config.save_config") as save, \
             patch("niji.setup_wizard.arrow_select", return_value="custom"), \
             patch("niji.setup_wizard._wizard_custom_provider", return_value=("example", custom)), \
             patch("niji.setup_wizard.test_connection", return_value=(False, "HTTP 401 Unauthorized")), \
             patch("niji.ui.render_setup_banner"), \
             patch("niji.setup_wizard.console", Console(file=io.StringIO(), color_system=None)):
            with self.assertRaises(SystemExit):
                run_setup()
        save.assert_not_called()
        self.assertEqual(cfg, {"provider": "custom", "custom_providers": {}})


class DocumentReadingTests(unittest.TestCase):
    def _make_office_file(self, path, members):
        with zipfile.ZipFile(path, "w") as archive:
            for name, text in members.items():
                archive.writestr(name, text)

    def test_reads_docx_as_untrusted_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.docx"
            self._make_office_file(path, {
                "word/document.xml": '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Build instructions</w:t></w:r></w:p><w:p><w:r><w:t>Run tests</w:t></w:r></w:p></w:body></w:document>'
            })
            result = read_document(str(path))
        self.assertIn("Untrusted document text", result)
        self.assertIn("Build instructions", result)
        self.assertIn("Run tests", result)

    def test_reads_pptx_slides_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slides.pptx"
            self._make_office_file(path, {
                "ppt/slides/slide2.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:sld>',
                "ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>First</a:t></a:r></a:p></p:sld>',
            })
            result = read_document(str(path))
        self.assertLess(result.index("First"), result.index("Second"))

    def test_reads_xlsx_shared_strings_and_bounded_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.xlsx"
            self._make_office_file(path, {
                "xl/sharedStrings.xml": '<sst xmlns="urn:s"><si><t>Feature</t></si><si><t>Ready</t></si></sst>',
                "xl/worksheets/sheet1.xml": '<worksheet xmlns="urn:s"><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row></sheetData></worksheet>',
            })
            result = read_document(str(path))
        self.assertIn("Feature\tReady", result)

    def test_rejects_unsupported_and_oversized_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            unsupported = Path(directory) / "notes.txt"
            unsupported.write_text("hello")
            oversized = Path(directory) / "large.pdf"
            oversized.write_bytes(b"x" * 20_000_001)
            self.assertIn("supported formats", read_document(str(unsupported)))
            self.assertIn("20 MB", read_document(str(oversized)))

    def test_pdf_without_optional_reader_has_install_guidance(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF")
            with patch.dict(sys.modules, {"pypdf": None}):
                result = read_document(str(path))
        self.assertIn("[documents]", result)

    def test_document_text_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long.docx"
            paragraph = "x" * 600
            self._make_office_file(path, {
                "word/document.xml": f'<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p></w:body></w:document>'
            })
            result = read_document(str(path), max_chars=500)
        self.assertTrue(result.endswith("[truncated at 500 characters]"))

    def test_document_tool_is_registered_and_read_only_for_explore(self):
        names = {schema["function"]["name"] for schema in CORE_SCHEMAS}
        self.assertIn("read_document", names)
        self.assertIn("read_document", HANDLERS)
        self.assertIn("read_document", READ_ONLY_SUBAGENT_TOOLS)
        self.assertEqual(HANDLERS["read_document"].__name__, "read_document")


if __name__ == "__main__":
    unittest.main()
