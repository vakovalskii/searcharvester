"""Hermes v0.21 asks the ACP client before every edit: only the job workspace is writable."""

from pathlib import Path

import permissions


class Opt:
    def __init__(self, option_id, kind):
        self.option_id, self.kind = option_id, kind


OPTIONS = [Opt("allow_once", "allow_once"), Opt("allow_session", "allow_always"), Opt("deny", "reject_once")]


def test_edit_inside_workspace_is_allowed(tmp_path: Path):
    ok, _ = permissions.decide(tmp_path, {"kind": "edit", "locations": [{"path": "report.md"}]})
    assert ok
    ok, _ = permissions.decide(tmp_path, {"kind": "edit", "raw_input": {"path": str(tmp_path / "extracts" / "a.md")}})
    assert ok
    assert permissions.pick_option(OPTIONS, True) == "allow_once"


def test_edit_outside_workspace_is_denied(tmp_path: Path):
    for p in ("/etc/passwd", "../other-job/report.md", "/opt/data/skills/x/SKILL.md"):
        ok, reason = permissions.decide(tmp_path, {"kind": "edit", "locations": [{"path": p}]})
        assert not ok and "outside" in reason
    assert permissions.pick_option(OPTIONS, False) == "deny"


def test_symlink_escape_is_denied(tmp_path: Path):
    (tmp_path / "link").symlink_to("/etc")
    ok, _ = permissions.decide(tmp_path, {"kind": "edit", "locations": [{"path": "link/passwd"}]})
    assert not ok


def test_request_without_path_or_not_an_edit_is_denied(tmp_path: Path):
    assert not permissions.decide(tmp_path, {"kind": "edit"})[0]
    assert not permissions.decide(tmp_path, {"kind": "execute", "raw_input": {"command": "rm -r /"}})[0]
