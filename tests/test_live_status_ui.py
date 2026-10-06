from pathlib import Path


def test_local_live_status_is_one_line_with_white_shimmer():
    source = (Path(__file__).parents[1] / "src/niji/webui_frontend.py").read_text()

    assert ".message.live-status .workmeta{display:block" in source
    assert "white-space:nowrap;overflow:hidden;text-overflow:ellipsis" in source
    assert ".message.live-status:not(.paused) .workmeta" in source
    assert "linear-gradient(100deg,var(--muted) 0%,var(--muted) 42%,#fff 50%" in source
    assert "animation:niji-live-status-shimmer 2.1s linear infinite" in source
    assert "@keyframes niji-live-status-shimmer" in source
    assert ".message.live-status.paused .workmeta" in source
    assert ".message.live-status .run-activity{display:none!important}" in source
    assert "prefers-reduced-motion:reduce" in source
    assert "background:none;-webkit-text-fill-color:currentColor" in source
