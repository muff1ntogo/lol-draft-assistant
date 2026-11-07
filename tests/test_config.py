from pathlib import Path

from draftassist.config import load_settings, parse_env_file

FAKE_KEY = "RGAPI-00000000-test-key"


def test_parse_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nRIOT_API_KEY=abc\nexport QUOTED=\"hello world\"\nSINGLE='x'\nnoequals\n",
        encoding="utf-8",
    )
    assert parse_env_file(env) == {"RIOT_API_KEY": "abc", "QUOTED": "hello world", "SINGLE": "x"}


def test_missing_env_file_is_empty(tmp_path: Path):
    assert parse_env_file(tmp_path / "nope.env") == {}


def test_load_settings_reads_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"RIOT_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.riot_api_key == FAKE_KEY
    assert settings.has_riot_api_key


def test_environment_wins_over_env_file(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("RIOT_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("RIOT_API_KEY", "from-env")
    assert load_settings(env).riot_api_key == "from-env"


def test_empty_key_means_no_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("RIOT_API_KEY=\n", encoding="utf-8")
    assert not load_settings(env).has_riot_api_key


def test_key_never_in_repr_or_str(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    settings = load_settings(tmp_path / "nope.env")
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
