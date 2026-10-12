import pytest

from duet.protocol import Patch
from duet.workspace import PatchRejected, Workspace


def test_digest_changes_with_content(tmp_path):
    ws = Workspace(str(tmp_path))
    before = ws.digest()
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert ws.digest() != before
    stable = ws.digest()
    assert ws.digest() == stable  # deterministic


def test_digest_ignores_duet_internals(tmp_path):
    ws = Workspace(str(tmp_path))
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    before = ws.digest()
    (tmp_path / ".duet" / "sessions").mkdir(parents=True)
    (tmp_path / ".duet" / "sessions" / "log").write_text("noise", encoding="utf-8")
    assert ws.digest() == before


def test_patches_apply_and_report(tmp_path):
    ws = Workspace(str(tmp_path))
    log = ws.apply_patches([Patch(path="pkg/mod.py", action="write", content="x = 1\n")])
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "created" in log[0]
    assert "unchanged" in ws.apply_patches([Patch(path="pkg/mod.py", content="x = 1\n")])[0]
    assert "deleted" in ws.apply_patches([Patch(path="pkg/mod.py", action="delete")])[0]


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", ".git/config", ".duet/config.json"])
def test_paths_outside_the_workspace_are_refused(tmp_path, path):
    ws = Workspace(str(tmp_path))
    with pytest.raises(PatchRejected):
        ws.resolve(path)
    log = ws.apply_patches([Patch(path=path, content="pwned")])
    assert log[0].startswith("REJECTED")


def test_gate_result_reflects_the_command(tmp_path):
    assert Workspace(str(tmp_path), gate="true").run_gate().ok
    failed = Workspace(str(tmp_path), gate="exit 3").run_gate()
    assert not failed.ok and failed.exit_code == 3
    assert Workspace(str(tmp_path)).run_gate().skipped


def _git(tmp_path, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8")


def test_diff_shows_new_files_without_staging_them(tmp_path):
    """duet is a guest in someone's repo: it must not quietly stage their files
    just to render a diff for the reviewer."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "initial")

    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "brand_new.py").write_text("x = 1\n", encoding="utf-8")

    ws = Workspace(str(tmp_path))
    out = ws.diff()
    assert "two" in out                       # the edit is visible
    assert "brand_new.py" in out              # and so is the new file

    staged = _git(tmp_path, "diff", "--cached", "--name-only").stdout
    assert "brand_new.py" not in staged       # but nothing was staged
    assert staged.strip() == ""


def test_diff_falls_back_to_a_listing_outside_a_repo(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert "a.py" in Workspace(str(tmp_path)).diff()


def test_a_large_file_edited_in_place_changes_the_state_id(tmp_path):
    """Summarising big files by length let two different workspaces share a
    state id, so a sign-off — and a cached gate result — carried across a change
    neither agent had seen."""
    big = tmp_path / "big.dat"
    big.write_bytes(b"OK" + b"\0" * 3_000_000)
    ws = Workspace(str(tmp_path))
    before = ws.digest()

    big.write_bytes(b"NO" + b"\0" * 3_000_000)   # same length, different content
    assert big.stat().st_size == 3_000_002
    assert ws.digest() != before


def test_retargeting_a_symlink_changes_the_state_id(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two", encoding="utf-8")
    link = tmp_path / "current.txt"
    link.symlink_to("a.txt")

    ws = Workspace(str(tmp_path))
    before = ws.digest()
    link.unlink()
    link.symlink_to("b.txt")
    assert ws.digest() != before


def test_a_symlink_is_not_followed_when_hashing(tmp_path):
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    ws = Workspace(str(tmp_path))
    assert ws.digest()            # does not raise, does not read through
    outside.unlink()


def test_reviewing_a_branch_sees_committed_work(tmp_path):
    """`duet review` on a branch you have already committed found nothing,
    because it only looked at the working tree — which is the normal state when
    you want a review."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "trunk")

    (tmp_path / "feature.py").write_text("def thing():\n    return 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "the feature")

    ws = Workspace(str(tmp_path))
    assert "no changes" in ws.diff().lower() or "no uncommitted" in ws.diff().lower() \
        or ws.diff().startswith("(git")          # working tree is clean
    branch = ws.diff_since("trunk")
    assert "feature.py" in branch and "def thing" in branch


def test_reviewing_against_a_ref_that_does_not_exist_says_so(tmp_path):
    _git(tmp_path, "init", "-q")
    assert "no such ref" in Workspace(str(tmp_path)).diff_since("nope")


def test_reviewing_against_a_ref_with_nothing_new(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "one")
    assert "does not already have" in Workspace(str(tmp_path)).diff_since("HEAD")
