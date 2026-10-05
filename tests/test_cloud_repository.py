import io
import zipfile
import unittest

import httpx

from niji.cloud_repository import (
    RepositoryImportError,
    fetch_github_repository,
    validate_repository_spec,
)


REVISION = "a" * 40


class GitHubRepositoryImportTests(unittest.TestCase):
    @staticmethod
    def archive(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries:
                zf.writestr(name, content)
        return buffer.getvalue()

    @staticmethod
    def spec(url="https://github.com/example/demo", revision=REVISION):
        return {"url": url, "revision": revision}

    def test_validates_and_normalizes_only_public_pinned_github_sources(self):
        self.assertEqual(
            validate_repository_spec(self.spec("https://github.com/Example/demo.git")),
            {"url": "https://github.com/Example/demo", "revision": REVISION},
        )
        for invalid in (
            self.spec("http://github.com/example/demo"),
            self.spec("https://github.com.evil.example/example/demo"),
            self.spec("https://user:pass@github.com/example/demo"),
            self.spec("https://github.com/example/demo?ref=main"),
            self.spec("https://github.com/example/demo/tree/main"),
            self.spec("https://github.com/example/../demo"),
            self.spec(revision="main"),
            {"url": "https://github.com/example/demo", "revision": REVISION, "token": "secret"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_repository_spec(invalid)

    def test_downloads_from_fixed_host_and_strips_github_archive_root(self):
        calls = []
        archive = self.archive([
            ("demo-aaaa/README.md", "# safe project\n"),
            ("demo-aaaa/src/main.py", "print('hello')\n"),
        ])

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=archive, headers={"content-type": "application/zip"})

        result = fetch_github_repository(self.spec(), transport=httpx.MockTransport(handler))
        self.assertEqual(calls[0].url.scheme, "https")
        self.assertEqual(calls[0].url.host, "codeload.github.com")
        self.assertEqual(calls[0].url.path, f"/example/demo/zip/{REVISION}")
        self.assertNotIn("authorization", calls[0].headers)
        self.assertEqual(result, [
            {"path": "README.md", "content": "# safe project\n"},
            {"path": "src/main.py", "content": "print('hello')\n"},
        ])

    def test_redirects_private_repositories_and_oversized_archives_are_rejected(self):
        for response in (
            httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"}),
            httpx.Response(404),
            httpx.Response(200, content=b"x" * 500_001),
        ):
            with self.subTest(status=response.status_code), self.assertRaises(RepositoryImportError):
                fetch_github_repository(
                    self.spec(), transport=httpx.MockTransport(lambda _request, r=response: r)
                )

    def test_archive_paths_secrets_and_binary_files_are_rejected(self):
        archives = (
            self.archive([("demo-root/../escape.txt", "x")]),
            self.archive([("demo-root/.env", "TOKEN=secret")]),
            self.archive([("demo-root/image.bin", b"\x00\xff")]),
        )
        for archive in archives:
            with self.subTest(size=len(archive)), self.assertRaises(RepositoryImportError):
                fetch_github_repository(
                    self.spec(),
                    transport=httpx.MockTransport(lambda _request, data=archive: httpx.Response(200, content=data)),
                )

    def test_cancellation_is_checked_while_streaming(self):
        archive = self.archive([("demo-root/main.py", "print('ok')")])
        checks = []

        def cancelled():
            checks.append(True)
            raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            fetch_github_repository(
                self.spec(),
                transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=archive)),
                check_cancelled=cancelled,
            )
        self.assertEqual(len(checks), 1)


if __name__ == "__main__":
    unittest.main()
