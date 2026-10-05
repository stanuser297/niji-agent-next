import json
import subprocess

import pytest

from niji import events, worktree


def test_event_log_roundtrip_and_redaction(tmp_path):
    log = events.EventLog("s1", tmp_path)
    log.emit("tool.start", tool="bash", args={"command": "ls", "api_key": "sk-abcdefghijklmnop1234"})
    log.emit("activity", level="TOOL", message="token=abcdef123456 used")
    log2 = events.EventLog("s1", tmp_path)
    got = log2.read()
    assert [e["seq"] for e in got] == [1, 2]
    raw = (tmp_path / "s1.jsonl").read_text()
    assert "abcdefghijklmnop1234" not in raw and "abcdef123456" not in raw
    assert log2.emit("activity", level="X", message="y")["seq"] == 3
    assert log2.read(since_seq=2)[0]["seq"] == 3
    assert log2.read(types={"tool.start"})[0]["data"]["tool"] == "bash"


def test_event_log_skips_corrupt_lines_and_rejects_bad_input(tmp_path):
    log = events.EventLog("s2", tmp_path)
    log.emit("run.cancel")
    with open(tmp_path / "s2.jsonl", "a") as fh:
        fh.write("not json\n")
    assert len(log.read()) == 1
    with pytest.raises(ValueError):
        log.emit("bogus.type")
    with pytest.raises(ValueError):
        events.EventLog("../evil", tmp_path)
    assert events.list_sessions(tmp_path) == ["s2"]
    assert (tmp_path / "s2.jsonl").stat().st_mode & 0o077 == 0


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("hi")
    run("add", ".")
    run("commit", "-qm", "init")
    return repo


def test_worktree_lifecycle(tmp_path):
    repo = _repo(tmp_path)
    target = worktree.create("feat-1", repo)
    assert (target / "a.txt").exists()
    names = [i["branch"] for i in worktree.list_worktrees(repo)]
    assert names == ["refs/heads/niji/feat-1"]
    with pytest.raises(worktree.WorktreeError):
        worktree.create("feat-1", repo)
    with pytest.raises(worktree.WorktreeError):
        worktree.create("../x", repo)
    worktree.remove("feat-1", repo)
    assert worktree.list_worktrees(repo) == []


def test_agent_emits_events(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "EVENTS_DIR", tmp_path)
    from niji.agent import Agent
    agent = Agent({"api_key": "x", "base_url": "http://127.0.0.1:1", "model": "m", "provider": "p"},
                  approval="ask")
    agent.events = events.EventLog(agent.session_id, tmp_path)
    agent.approval_callback = lambda name, args: False
    out = agent._execute({"name": "bash", "args": {"command": "echo hi"}, "id": "1"})
    assert "denied" in out
    kinds = [e["type"] for e in agent.events.read()]
    assert "approval.requested" in kinds and "approval.decided" in kinds
    assert json.loads(json.dumps(agent.events.read()))


def test_events_endpoint_and_view(tmp_path):
    import json as _json, urllib.request
    from niji.webui import NijiWebUI
    from niji.webui_frontend import PAGE

    class A:
        events = events.EventLog("web1", tmp_path)
        provider_name = "p"; model = "m"
    A.events.emit("run.cancel")
    ui = NijiWebUI(A(), host="127.0.0.1", port=0)
    assert 'id="view-events"' in PAGE
    import threading
    httpd = ui.httpd if hasattr(ui, "httpd") else None
    assert httpd is not None and httpd.server_address[0] == "127.0.0.1"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/events",
                                 headers={"X-Niji-Token": ui.token})
    body = _json.load(urllib.request.urlopen(req))
    assert body["events"][0]["type"] == "run.cancel"
    httpd.shutdown()
