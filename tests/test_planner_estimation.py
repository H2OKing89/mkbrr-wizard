from __future__ import annotations

from pathlib import Path

from mkbrr_wizard.planning.planner import _estimate_content


def test_estimate_content_handles_deeply_nested_directories(tmp_path: Path) -> None:
    nested = tmp_path
    for _ in range(1_100):
        nested /= "d"
        nested.mkdir()
    (nested / "release.mkv").write_bytes(b"content")

    assert _estimate_content(str(tmp_path)) == (1, len(b"content"))
