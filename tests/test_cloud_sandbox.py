import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from niji.cloud_runtime import RunCancelled, RunRequest
from niji.cloud_worker import CloudRunContext, WorkerSettings
from niji.cloud_sandbox import E2BSandboxExecutor, _RemoteWorkspaceTools
from niji.agent import Agent


class FakeSandbox:
    last = None
    create_args = None

    @classmethod
    def create(cls, **kwargs):
        cls.create_args = kwargs
        cls.last = cls()
        return cls.last

    def __init__(self):
        self.killed = 0
        self.commands = types.SimpleNamespace(run=self.run)
        self.files = types.SimpleNamespace(
            write=self.write, list=self.list_files, read=self.read,
        )
        self.writes = []
        self.entries = []
        self.contents = {}

    def list_files(self, _path, **_kwargs):
        return self.entries

    def read(self, path, format="text"):
        value = self.contents[path]
        return value if format == "bytes" else value.decode("utf-8")

    def run(self, command, **kwargs):
        return types.SimpleNamespace(stdout="", stderr="", exit_code=0)

    def write(self, path, content):
        self.writes.append((path, content))

    def kill(self):
        self.killed += 1
        return True


class CloudSandboxTests(unittest.TestCase):
    def settings(self):
        return WorkerSettings(
            database_url="postgresql://db-secret", tenant_id="tenant",
            provider="openai", base_url="https://model.example/v1", model="test",
            provider_api_key="provider-secret", max_turns=2, execution_mode="sandbox",
        )

    def test_sandbox_mode_uses_ephemeral_offline_sandbox_and_limited_tools(self):
        created = {}

        class FakeAgent:
            def __init__(self, provider, **kwargs):
                created["provider"] = provider
                created["kwargs"] = kwargs

            def chat(self, _prompt):
                dispatcher = created["kwargs"]["tool_dispatcher"]
                self.tool_result = dispatcher("write_file", {
                    "path": "src/main.py", "content": "print('hello')\n"
                }, {})
                return "Finished safely"

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}), \
                patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=FakeSandbox)}), \
                patch("niji.agent.Agent", FakeAgent):
            context = CloudRunContext(__import__("time").monotonic() + 60)
            executor = E2BSandboxExecutor(self.settings())
            result = executor(RunRequest("sandbox-test", {"prompt": "make a tiny script", "files": [{"path": "README.md", "content": "hello"}]}), context)

        self.assertEqual(result, {"text": "Finished safely"})
        self.assertEqual(FakeSandbox.create_args["allow_internet_access"], False)
        self.assertNotIn("envs", FakeSandbox.create_args)
        self.assertEqual(FakeSandbox.last.killed, 1)
        self.assertEqual(FakeSandbox.last.writes, [
            ("/tmp/niji-workspace/README.md", "hello"),
            ("/tmp/niji-workspace/src/main.py", "print('hello')\n"),
        ])
        kwargs = created["kwargs"]
        self.assertTrue(kwargs["cloud_mode"])
        self.assertEqual(set(kwargs["allowed_tools"]), {"bash", "read_file", "write_file", "edit_file", "list_files"})
        self.assertNotIn("provider-secret", repr(FakeSandbox.create_args))
        self.assertNotIn("db-secret", repr(FakeSandbox.create_args))

    def test_repository_revision_is_imported_before_network_disabled_sandbox_starts(self):
        imported = [{"path": "src/main.py", "content": "print('imported')\n"}]
        class RepoAgent:
            def __init__(self, *_args, **_kwargs):
                pass
            def chat(self, _prompt):
                return "Reviewed pinned repository"

        request = RunRequest("repo-sandbox", {
            "prompt": "review this commit",
            "repository": {"url": "https://github.com/example/demo", "revision": "a" * 40},
        })
        with patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}), \
                patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=FakeSandbox)}), \
                patch("niji.cloud_repository.fetch_github_repository", return_value=imported) as fetch, \
                patch("niji.agent.Agent", RepoAgent):
            context = CloudRunContext(__import__("time").monotonic() + 60)
            result = E2BSandboxExecutor(self.settings())(request, context)
        fetch.assert_called_once_with(request.payload["repository"], check_cancelled=context.check_cancelled)
        self.assertEqual(result, {"text": "Reviewed pinned repository"})
        self.assertEqual(FakeSandbox.last.writes, [
            ("/tmp/niji-workspace/src/main.py", "print('imported')\n"),
        ])
        self.assertFalse(FakeSandbox.create_args["allow_internet_access"])
        self.assertEqual(FakeSandbox.last.killed, 1)

    def test_sandbox_is_killed_when_agent_fails(self):
        class FailingAgent:
            def __init__(self, *_args, **_kwargs):
                pass

            def chat(self, _prompt):
                raise RuntimeError("private error")

        with patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}), \
                patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=FakeSandbox)}), \
                patch("niji.agent.Agent", FailingAgent):
            context = CloudRunContext(__import__("time").monotonic() + 60)
            with self.assertRaisesRegex(RuntimeError, "private error"):
                E2BSandboxExecutor(self.settings())(
                    RunRequest("cleanup-test", {"prompt": "run"}), context
                )
        self.assertEqual(FakeSandbox.last.killed, 1)

    def test_cancellation_during_sandbox_setup_kills_remote_sandbox(self):
        context = CloudRunContext(__import__("time").monotonic() + 60)

        class CancellingSandbox(FakeSandbox):
            @classmethod
            def create(cls, **kwargs):
                cls.create_args = kwargs
                cls.last = cls()
                return cls.last

            def run(self, command, **kwargs):
                context.cancel()
                return types.SimpleNamespace(stdout="", stderr="", exit_code=0)

        with patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}), \
                patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=CancellingSandbox)}), \
                patch("niji.agent.Agent") as agent_class:
            with self.assertRaises(RunCancelled):
                E2BSandboxExecutor(self.settings())(
                    RunRequest("cancel-setup", {"prompt": "run"}), context
                )
        agent_class.assert_not_called()
        self.assertEqual(CancellingSandbox.last.killed, 1)

    def test_path_traversal_is_rejected_before_remote_file_write(self):
        with tempfile.TemporaryDirectory() as temp:
            sandbox = FakeSandbox()
            bridge = _RemoteWorkspaceTools(sandbox, Path(temp))
            result = bridge.dispatch("write_file", {"path": "../escape.txt", "content": "x"})
            self.assertEqual(result, "[sandbox tool error] ValueError")
            self.assertEqual(sandbox.writes, [])

    def test_read_file_uses_nofollow_for_files_and_parent_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "niji-workspace"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("sandbox-private-secret", encoding="utf-8")
            (root / "file-link").symlink_to(secret)
            (root / "dir-link").symlink_to(outside, target_is_directory=True)

            class LocalCommandSandbox:
                def __init__(self):
                    self.commands = types.SimpleNamespace(run=self.run)

                @staticmethod
                def run(command, **kwargs):
                    completed = subprocess.run(
                        command, shell=True, capture_output=True, text=True,
                        timeout=kwargs.get("timeout", 30),
                    )
                    if kwargs.get("on_stdout"):
                        kwargs["on_stdout"](completed.stdout)
                    if kwargs.get("on_stderr"):
                        kwargs["on_stderr"](completed.stderr)
                    return types.SimpleNamespace(
                        stdout=completed.stdout, stderr=completed.stderr,
                        exit_code=completed.returncode,
                    )

            with patch("niji.cloud_sandbox._SANDBOX_ROOT", str(root)):
                bridge = _RemoteWorkspaceTools(LocalCommandSandbox(), base)
                for path in ("file-link", "dir-link/secret.txt"):
                    result = bridge.dispatch("read_file", {"path": path})
                    self.assertNotIn("sandbox-private-secret", result)
                    self.assertIn("error", result)

    def test_tool_output_and_command_timeouts_are_bounded(self):
        class Commands:
            def __init__(self):
                self.kwargs = None

            def run(self, _command, **kwargs):
                self.kwargs = kwargs
                kwargs["on_stdout"]("x" * 50_000)
                return types.SimpleNamespace(stdout="x" * 50_000, stderr="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp:
            sandbox = FakeSandbox()
            commands = Commands()
            sandbox.commands.run = commands.run
            bridge = _RemoteWorkspaceTools(sandbox, Path(temp))
            result = bridge.dispatch("bash", {"command": "echo test", "timeout": 9999})
            self.assertLessEqual(len(result), 20_000)
            self.assertLessEqual(commands.kwargs["timeout"], 120)

    def test_agent_routes_allowlisted_tool_calls_to_custom_dispatcher(self):
        calls = []
        def bridge(name, args, _ctx):
            calls.append((name, args))
            return "remote response"

        with (
            tempfile.TemporaryDirectory() as workspace,
            patch("niji.agent.OpenAI"),
            patch("niji.agent.load_config", return_value={}),
            patch("niji.agent.load_plan", return_value=[]),
            patch("niji.agent.load_project_guidance", return_value=""),
            patch("niji.agent.discover_skills", return_value=[]),
        ):
            agent = Agent(
                {"provider": "openai", "base_url": "https://model.example/v1",
                 "model": "test", "api_key": "private"},
                approval="auto", allowed_tools=["bash"], workspace=workspace,
                cloud_mode=True, tool_dispatcher=bridge, verbose=False,
            )
            result = agent._execute({"name": "bash", "args": {"command": "echo hello"}})
        self.assertEqual(result, "remote response")
        self.assertEqual(calls, [("bash", {"command": "echo hello"})])

    def test_generated_artifacts_are_bounded_and_secrets_are_omitted(self):
        artifacts = []
        class ArtifactAgent:
            def __init__(self, *_args, **_kwargs):
                pass
            def chat(self, _prompt):
                return "ready"

        sandbox = FakeSandbox()
        sandbox.entries = [
            types.SimpleNamespace(path="/tmp/niji-workspace/out/report.txt", type=types.SimpleNamespace(value="file"), size=5, symlink_target=None),
            types.SimpleNamespace(path="/tmp/niji-workspace/.env", type=types.SimpleNamespace(value="file"), size=5, symlink_target=None),
            types.SimpleNamespace(path="/tmp/niji-workspace/link.txt", type=types.SimpleNamespace(value="symlink"), size=5, symlink_target="/etc/passwd"),
        ]
        sandbox.contents = {
            "/tmp/niji-workspace/out/report.txt": b"hello",
            "/tmp/niji-workspace/.env": b"TOKEN",
        }

        class ArtifactSandbox(FakeSandbox):
            @classmethod
            def create(cls, **kwargs):
                cls.create_args = kwargs
                cls.last = sandbox
                return sandbox

        with patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}), \
                patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=ArtifactSandbox)}), \
                patch("niji.agent.Agent", ArtifactAgent):
            executor = E2BSandboxExecutor(self.settings())
            executor.set_artifact_callback(lambda *args: artifacts.append(args))
            result = executor(
                RunRequest("artifacts-test", {"prompt": "produce a report"}),
                CloudRunContext(__import__("time").monotonic() + 60),
            )
        self.assertEqual(result, {"text": "ready"})
        self.assertEqual(artifacts, [("out/report.txt", b"hello", "text/plain")])
        self.assertEqual(sandbox.killed, 1)

    def test_non_prompt_payload_is_rejected(self):
        with patch.dict(os.environ, {"E2B_API_KEY": "e2b-secret"}):
            executor = E2BSandboxExecutor(self.settings())
            with self.assertRaisesRegex(ValueError, "prompt and at most one bounded project source"):
                executor(RunRequest("extra-field", {"prompt": "hi", "command": "bad"}),
                         CloudRunContext(__import__("time").monotonic() + 60))


if __name__ == "__main__":
    unittest.main()
