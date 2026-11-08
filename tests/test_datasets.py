"""Stage 4 (docs/shared-data.md): admin-published datasets with their own fits, and the user's selection: additive tables
summed for exactly that selection, fits from the closest published dataset."""
import pytest
from sqlalchemy import text
from sqlmodel import Session

from draftassist.data.aggregate import aggregate
from draftassist.data.datasets import (
    DEFAULT_DATASETS,
    SEL,
    Dataset,
    Selection,
    apply_selection,
    closest_dataset,
    ensure_datasets,
    fit_datasets,
    fits_label,
    load_datasets,
    load_selection,
    save_selection,
    window_of,
)
from draftassist.data.shared.publish import publish
from draftassist.data.shared.pull import pull
from draftassist.data.shared.store import FakeSharedStore
from draftassist.db.models import Champion, Player
from draftassist.db.session import init_db
from test_counts import crawl

PATCHES = ("16.19", "16.20", "16.21")


def champions(db):
    with Session(db) as s:
        for c in range(1, 41):
            s.merge(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()


@pytest.fixture
def admin(tmp_path):
    db = init_db(tmp_path / "admin" / "draftassist.sqlite")
    crawl(db, n=240, patches=PATCHES, brackets=("gold", "iron", "silver"), queues=(420, 440))
    for p in PATCHES:
        aggregate(db, p, window="16.21")
    return db


@pytest.fixture
def user(tmp_path, admin):
    db = init_db(tmp_path / "user" / "draftassist.sqlite")
    champions(db)
    with Session(db) as s:  # our gold player: the bracket Live reads
        s.add(Player(puuid="unresolved:mid", riot_id="Me#EUW", role="mid", rank="GOLD II", bracket="gold"))
        s.commit()
    store = FakeSharedStore()
    ensure_datasets(admin)
    fit_datasets(admin)
    publish(admin, None, store, app_version="x", log=lambda *_: None)
    pull(db, store, log=lambda *_: None)
    return db


def direct(admin, patches, tiers, queues):
    """The reference: champion games and wins straight from the matches of exactly this selection."""
    marks = lambda xs, p: ", ".join(f":{p}{i}" for i in range(len(xs)))  # noqa: E731
    params = {**{f"p{i}": x for i, x in enumerate(patches)}, **{f"t{i}": x for i, x in enumerate(tiers)},
              **{f"q{i}": x for i, x in enumerate(queues)}}
    with admin.connect() as conn:
        return sorted(conn.execute(text(
            f"SELECT mp.champ_id, mp.role, COUNT(*), SUM(mp.win) FROM match_participants mp JOIN crawled_matches cm "
            f"ON cm.match_id = mp.match_id WHERE cm.patch IN ({marks(patches, 'p')}) AND cm.bracket IN "
            f"({marks(tiers, 't')}) AND cm.queue_id IN ({marks(queues, 'q')}) GROUP BY mp.champ_id, mp.role"), params).all())


def selected(user, bracket="gold"):
    with user.connect() as conn:
        return sorted(conn.execute(text("SELECT champ_id, role, games, wins FROM champ_role_stats "
                                        "WHERE patch = :p AND bracket = :b"), {"p": SEL, "b": bracket}).all())


# ---------- the selection's additive tables ----------
def test_a_multi_patch_multi_tier_selection_sums_to_a_direct_aggregation(user, admin):
    sel = Selection(scope="patches", patches=["16.20", "16.21"], half_life=0, queues=[420, 440],
                    tiers={"gold": ["gold", "iron"]})
    save_selection(user, sel)
    apply_selection(user)
    got = [(c, r, round(g), round(w)) for c, r, g, w in selected(user)]
    assert got == direct(admin, ["16.20", "16.21"], ["gold", "iron"], [420, 440])
    with user.connect() as conn:
        n = conn.execute(text("SELECT matches FROM bracket_totals WHERE patch = :p AND bracket = 'gold'"),
                         {"p": SEL}).scalar()
        ref = conn.execute(text("SELECT SUM(matches) FROM cnt_bracket WHERE patch IN ('16.20', '16.21') "
                                "AND bracket IN ('gold', 'iron')")).scalar()
    assert round(n) == ref


def test_queues_filter_and_last_n_weights(user, admin):
    save_selection(user, Selection(scope="last_n", n=2, half_life=0, queues=[440], tiers={"gold": ["gold"]}))
    apply_selection(user)
    got = [(c, r, round(g), round(w)) for c, r, g, w in selected(user)]
    assert got == direct(admin, ["16.20", "16.21"], ["gold"], [440])
    save_selection(user, Selection(scope="last_n", n=2, half_life=1, queues=[420, 440], tiers={"gold": ["gold"]}))
    apply_selection(user)
    with user.connect() as conn:  # 16.21 counts fully, 16.20 half
        total = conn.execute(text("SELECT matches FROM bracket_totals WHERE patch = :p AND bracket = 'gold'"),
                             {"p": SEL}).scalar()
        a = conn.execute(text("SELECT SUM(matches) FROM cnt_bracket WHERE patch = '16.21' AND bracket = 'gold'")).scalar()
        b = conn.execute(text("SELECT SUM(matches) FROM cnt_bracket WHERE patch = '16.20' AND bracket = 'gold'")).scalar()
    assert total == pytest.approx(a + 0.5 * b)


def test_the_default_selection_is_todays_behaviour(user, admin):
    assert load_selection(user) == Selection()
    apply_selection(user)
    with user.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM champ_role_stats WHERE patch = :p"), {"p": SEL}).scalar() == 0
    from draftassist.data.synergy import latest_window

    assert latest_window(user) == "16.21" == latest_window(admin)


def test_the_engine_reads_the_selection_and_falls_back_like_any_low_data_bracket(user):
    from draftassist.data.provider import bracket_stats

    save_selection(user, Selection(scope="all", half_life=0, tiers={"gold": ["gold", "silver"]}))
    apply_selection(user)
    with user.connect() as conn:
        n = conn.execute(text("SELECT matches FROM bracket_totals WHERE patch = :p AND bracket = 'gold'"),
                         {"p": SEL}).scalar()
        ref = conn.execute(text("SELECT SUM(matches) FROM bracket_totals WHERE patch <> :p AND bracket IN ('gold', "
                                "'silver')"), {"p": SEL}).scalar()
    assert n == ref
    with Session(user) as s:
        stats, crawled = bracket_stats(s, SEL, "gold", None)
        assert crawled == n and stats.meta  # the engine's view of the selection
        # a selection with no games in the player's tiers is exactly a bracket the crawl hasn't reached
        save_selection(user, Selection(scope="all", half_life=0, tiers={"gold": ["diamond"]}))
    apply_selection(user)
    with Session(user) as s:
        assert bracket_stats(s, SEL, "gold", None) == bracket_stats(s, "16.21", "diamond", None) == (None, 0)


# ---------- datasets and their fits ----------
def test_the_admin_publishes_the_default_datasets_with_their_own_fits(admin, user):
    ids = [d.dataset_id for d in load_datasets(admin)]
    assert ids == [d.dataset_id for d in DEFAULT_DATASETS] == ["current", "recent", "all", "master_plus"]
    with user.connect() as conn:  # pulled
        windows = {r[0] for r in conn.execute(text('SELECT DISTINCT "window" FROM matchup_fits'))}
        traits = {r[0] for r in conn.execute(text('SELECT DISTINCT "window" FROM champion_traits'))}
        assert {d.dataset_id for d in load_datasets(user)} == set(ids)
    for ds in ("current", "recent", "all"):
        assert window_of(ds) in windows and window_of(ds) in traits
    with admin.connect() as conn:  # the datasets' synergy windows are only for fitting: not kept
        kept = {r[0] for r in conn.execute(text('SELECT DISTINCT "window" FROM synergy_role_stats'))}
    assert kept == {"16.21"}


def test_fitting_the_datasets_again_without_new_data_does_nothing(admin):
    ensure_datasets(admin)
    assert fit_datasets(admin) is True
    assert fit_datasets(admin) is False


def test_the_closest_dataset_is_deterministic():
    patches = ["16.17", "16.18", "16.19", "16.20", "16.21"]
    ds = list(DEFAULT_DATASETS) + [Dataset("last3", "Last 3 patches", "last_n", n=3)]
    pick = lambda s: closest_dataset(s, ds, patches, "16.21").dataset_id  # noqa: E731
    assert pick(Selection(scope="current")) == "current"
    assert pick(Selection(scope="all")) == "all"
    assert pick(Selection(scope="last_n", n=6)) == "recent"
    assert pick(Selection(scope="last_n", n=3)) == "last3"
    assert pick(Selection(scope="last_n", n=4)) == "last3"  # closer in patches than recent's 6 (here 5)
    assert pick(Selection(scope="patches", patches=["16.21"])) == "current"
    assert pick(Selection(scope="patches", patches=["16.19", "16.20", "16.21"])) == "last3"
    for _ in range(3):  # same answer every time, whatever the order given
        assert closest_dataset(Selection(scope="last_n", n=3), list(reversed(ds)), patches, "16.21").dataset_id == "last3"
    assert pick(Selection(scope="last_n", n=3, master_tree=False)) == "last3"  # Master+ isn't a candidate for Live


def test_the_fits_from_label(user):
    save_selection(user, Selection(scope="last_n", n=6))
    out = apply_selection(user)
    assert out["fits_from"] == "Fits from: recent (6 patches)" == fits_label(load_datasets(user)[1])
    from draftassist.data.synergy import latest_window

    assert latest_window(user) == window_of("recent")
    with user.connect() as conn:  # the selection's synergy, under the dataset's window, beside its pulled fits
        assert conn.execute(text('SELECT COUNT(*) FROM synergy_pairs WHERE "window" = :w'),
                            {"w": window_of("recent")}).scalar() > 0
    save_selection(user, Selection())
    assert apply_selection(user)["fits_from"].startswith("Fits from: the admin's current window")
    assert latest_window(user) == "16.21"


def test_master_plus_for_the_tree_can_be_turned_off(user):
    from draftassist.data.master import tree_uses_master

    assert tree_uses_master(user) is True
    save_selection(user, Selection(master_tree=False))
    assert tree_uses_master(user) is False


# ---------- in the app ----------
def test_the_selection_is_a_users_setting_and_reloads_the_stats(user, tmp_path, make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.data.shared.account import AccountStore, MemorySecrets, encode_invite

    svc = make_services(db=user)
    svc.account_store = AccountStore(tmp_path / "acc", MemorySecrets())
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.put("/api/selection", json={"scope": "current"}).status_code == 403  # solo: nothing pulled
        svc.account_store.join(encode_invite("libsql://x.turso.io", "eyJ.read.only", "Mika", 4_000_000_000))
        before = svc.loaded_version
        out = client.put("/api/selection", json={"scope": "last_n", "n": 6, "half_life": 6, "queues": [420, 440],
                                                "tiers": {"gold": ["gold", "silver"]}, "master_tree": True}).json()
        assert out["fits_from"] == "Fits from: recent (6 patches)" and out["selection"]["n"] == 6
        assert svc.loaded_version != before  # the stats reloaded
        got = client.get("/api/datasets").json()
        assert [d["dataset_id"] for d in got["datasets"]] == ["current", "recent", "all", "master_plus"]
        assert got["fits_from"] == "Fits from: recent (6 patches)"
        svc.in_draft = lambda: True
        assert client.put("/api/selection", json={"scope": "all"}).json()["reload"] == "deferred"
