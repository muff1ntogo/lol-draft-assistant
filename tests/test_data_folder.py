"""App window, stage 2: the installed app's data folder, and the batch jobs as subcommands."""
import importlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from draftassist import commands, launcher
from draftassist.config import (
    PROJECT_ROOT,
    USER_FILES,
    data_root,
    ensure_user_files,
    resource_root,
)


# ---------- where things live ----------
def test_a_source_checkout_keeps_everything_in_itself():
    assert data_root(frozen=False, env={}) == PROJECT_ROOT
    assert resource_root(frozen=False) == PROJECT_ROOT


def test_the_installed_app_writes_to_local_app_data():
    assert data_root(frozen=True, env={"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}) == \
        Path(r"C:\Users\me\AppData\Local") / "DraftAssistant"


def test_draftassist_home_wins_everywhere(tmp_path):
    env = {"DRAFTASSIST_HOME": str(tmp_path), "LOCALAPPDATA": "elsewhere"}
    assert data_root(frozen=True, env=env) == tmp_path
    assert data_root(frozen=False, env=env) == tmp_path


def test_the_installed_app_reads_its_files_from_the_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resource_root(frozen=True) == tmp_path


def test_every_file_the_app_writes_is_seeded_from_the_bundle():
    for rel in USER_FILES:
        assert (PROJECT_ROOT / rel).is_file(), rel


def test_user_files_are_copied_once_and_never_overwritten(tmp_path):
    bundle, data = tmp_path / "bundle", tmp_path / "data"
    for rel in USER_FILES:
        (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle / rel).write_text("bundled", encoding="utf-8")
    assert len(ensure_user_files(data, bundle)) == len(USER_FILES)
    edited = data / USER_FILES[0]
    edited.write_text("my edits", encoding="utf-8")
    assert ensure_user_files(data, bundle) == []
    assert edited.read_text(encoding="utf-8") == "my edits"


def test_in_a_checkout_seeding_is_a_no_op():
    assert ensure_user_files(PROJECT_ROOT, PROJECT_ROOT) == []


# ---------- moving a checkout's data across ----------
def make_checkout(root: Path, rows: int = 3) -> Path:
    (root / "data").mkdir(parents=True)
    db = sqlite3.connect(root / "data" / "draftassist.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x INTEGER)")
    db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    db.commit()  # left open: the rows may still be only in the WAL file, as with a running crawl
    (root / ".env").write_text("RIOT_API_KEY=not-a-real-key\n", encoding="utf-8")
    (root / "data" / "champion_tags.yaml").write_text("edited: true\n", encoding="utf-8")
    (root / "cache" / "riot").mkdir(parents=True)
    (root / "cache" / "riot" / "x.json").write_text("{}", encoding="utf-8")
    make_checkout.open = db  # the "running crawl"'s connection, for the test to write more or close
    return root


def rows(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        db.close()


def test_import_copies_the_database_key_and_edits(tmp_path):
    src = make_checkout(tmp_path / "checkout")
    dst = tmp_path / "app"
    copied = commands.import_data(src, dst)
    make_checkout.open.close()
    assert set(copied) == {"data/draftassist.sqlite", ".env", "data/champion_tags.yaml"}
    assert rows(dst / "data" / "draftassist.sqlite") == 3  # including what was only in the WAL
    assert (dst / ".env").read_text(encoding="utf-8").startswith("RIOT_API_KEY=")
    assert not (dst / "cache").exists()


def test_import_with_cache(tmp_path):
    src = make_checkout(tmp_path / "checkout")
    copied = commands.import_data(src, tmp_path / "app", with_cache=True)
    make_checkout.open.close()
    assert "cache/" in copied and (tmp_path / "app" / "cache" / "riot" / "x.json").is_file()


def test_import_never_replaces_a_database_unless_forced(tmp_path):
    src = make_checkout(tmp_path / "checkout")
    dst = tmp_path / "app"
    commands.import_data(src, dst)
    make_checkout.open.execute("INSERT INTO t VALUES (99)")
    make_checkout.open.commit()
    with pytest.raises(FileExistsError):
        commands.import_data(src, dst)
    commands.import_data(src, dst, force=True)
    make_checkout.open.close()
    assert rows(dst / "data" / "draftassist.sqlite") == 4


def test_import_needs_a_checkout(tmp_path):
    with pytest.raises(FileNotFoundError):
        commands.import_data(tmp_path / "nothing", tmp_path / "app")


def test_import_data_command_reports(tmp_path, monkeypatch, capsys):
    src = make_checkout(tmp_path / "checkout")
    monkeypatch.setattr("draftassist.config.DATA_ROOT", tmp_path / "app")
    assert launcher.main(["import-data", str(src)]) == 0
    assert launcher.main(["import-data", str(src)]) == 1  # already there
    make_checkout.open.close()
    out = capsys.readouterr().out
    assert "data/draftassist.sqlite" in out and "--force" in out
    assert "not-a-real-key" not in out  # the key is copied, never printed


# ---------- the jobs ----------
def test_every_job_has_a_main():
    for name, (module, _) in commands.COMMANDS.items():
        assert callable(getattr(importlib.import_module(module), "main", None)), name


def test_a_job_gets_its_arguments_as_python_m_would(monkeypatch):
    seen = {}

    def main():
        seen["argv"] = list(sys.argv)
        return 3

    monkeypatch.setitem(sys.modules, "fake_job", SimpleNamespace(main=main))
    monkeypatch.setitem(commands.COMMANDS, "fake", ("fake_job", "a test job"))
    before = list(sys.argv)
    assert launcher.main(["fake", "--hours", "8"]) == 3
    assert seen["argv"] == ["draftassist fake", "--hours", "8"]
    assert sys.argv == before


def test_a_job_returning_none_is_success(monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_job", SimpleNamespace(main=lambda: None))
    monkeypatch.setitem(commands.COMMANDS, "fake", ("fake_job", "a test job"))
    assert commands.run(["fake"]) == 0


def test_help_lists_the_jobs(capsys):
    with pytest.raises(SystemExit):
        launcher.main(["--help"])
    out = capsys.readouterr().out
    assert all(name in out for name in commands.COMMANDS) and "import-data" in out
