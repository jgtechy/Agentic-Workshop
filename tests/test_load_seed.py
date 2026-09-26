"""Tests for the seed loader (Epic 1, story 2). Every test uses a tmp_path DB."""

import csv
import importlib.util
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import load_seed

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "seed"


def read_csv(name: str) -> list[dict]:
    with (SEED_DIR / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def snapshot(db: Path) -> dict[str, list[tuple]]:
    return {
        "tickets": rows(db, "SELECT * FROM tickets ORDER BY ticket_id"),
        "customers": rows(db, "SELECT * FROM customers ORDER BY customer_id"),
    }


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "app.db"


@pytest.fixture
def seed_copy(tmp_path) -> Path:
    target = tmp_path / "seed"
    shutil.copytree(SEED_DIR, target)
    return target


def test_fresh_load(db):
    assert not db.exists()
    counts = load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    assert counts == {"tickets": 24, "customers": 20}
    assert rows(db, "SELECT count(*) FROM tickets") == [(24,)]
    assert rows(db, "SELECT count(*) FROM customers") == [(20,)]
    # Columns are exactly the CSV headers, in order, with the specified types and keys.
    assert [(c[1], c[2], c[5]) for c in rows(db, "PRAGMA table_info(tickets)")] == [
        ("ticket_id", "TEXT", 1), ("customer_id", "TEXT", 0), ("created_at", "TEXT", 0), ("text", "TEXT", 0),
    ]
    assert [(c[1], c[2], c[5]) for c in rows(db, "PRAGMA table_info(customers)")] == [
        ("customer_id", "TEXT", 1), ("name", "TEXT", 0), ("plan", "TEXT", 0), ("open_tickets", "INTEGER", 0),
    ]


def test_rerun_gives_identical_contents(db):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    first = snapshot(db)
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    assert snapshot(db) == first
    assert rows(db, "SELECT count(*) FROM tickets") == [(24,)]


def test_stale_row_is_removed(db):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    expected = snapshot(db)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO tickets VALUES ('T-9999', 'C-05', '2026-01-01T00:00:00', 'stale')")
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.execute("INSERT INTO other VALUES (7)")
    conn.close()
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    assert snapshot(db) == expected
    assert rows(db, "SELECT x FROM other") == [(7,)]  # other tables left alone


def test_open_tickets_is_integer(db):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    assert rows(db, "SELECT open_tickets, typeof(open_tickets) FROM customers WHERE customer_id = 'C-05'") == [
        (4, "integer")
    ]


def test_text_fidelity(db):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    source = {r["ticket_id"]: r["text"] for r in read_csv("tickets.csv")}
    assert any("," in text or '"' in text for text in source.values())
    stored = dict(rows(db, "SELECT ticket_id, text FROM tickets"))
    assert stored == source
    assert stored["T-1047"] == "Refund the duplicate charge, please."


def load_triage_server(db: Path | None = None):
    spec = importlib.util.spec_from_file_location("triage_server_under_test", REPO_ROOT / "mcp" / "triage_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if db is not None:
        module.DB_PATH = db
    return module


def test_default_db_path_matches_triage_server():
    assert load_seed.DEFAULT_DB_PATH == load_triage_server().DB_PATH


def test_mcp_tools_read_loaded_db(db):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    server = load_triage_server(db)
    ticket = server.get_ticket("T-1042")
    assert ticket["customer_id"] == "C-77"
    history = server.get_customer_history("C-77")
    assert "T-1042" in history["ticket_ids"]


def test_missing_seed_file_changes_nothing(db, seed_copy):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    before = db.read_bytes()
    (seed_copy / "customers.csv").unlink()
    with pytest.raises(FileNotFoundError, match="customers.csv"):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert db.read_bytes() == before


def test_missing_seed_file_creates_no_db(db, seed_copy):
    (seed_copy / "customers.csv").unlink()
    with pytest.raises(FileNotFoundError, match="customers.csv"):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert not db.exists()


def test_duplicate_id_changes_nothing(db, seed_copy):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    before = snapshot(db)
    tickets = seed_copy / "tickets.csv"
    with tickets.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["T-1042", "C-05", "2026-09-30T00:00:00", "duplicate"])
    with pytest.raises(sqlite3.IntegrityError, match="Loading table 'tickets'"):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert snapshot(db) == before


def test_duplicate_id_on_fresh_db_leaves_no_file(db, seed_copy):
    with (seed_copy / "tickets.csv").open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["T-1042", "C-05", "2026-09-30T00:00:00", "duplicate"])
    with pytest.raises(sqlite3.IntegrityError, match="Loading table 'tickets'"):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert not db.exists()



def test_second_table_failure_rolls_back_first(db, seed_copy):
    # tickets is rebuilt first, so a customers failure proves both tables share one transaction.
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    before = snapshot(db)
    tickets = seed_copy / "tickets.csv"
    tickets.write_text(tickets.read_text(encoding="utf-8").replace("Nothing is saving.", "changed", 1), encoding="utf-8")
    with (seed_copy / "customers.csv").open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["C-05", "Duplicate", "Free", "0"])
    with pytest.raises(sqlite3.IntegrityError, match="Loading table 'customers'"):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert snapshot(db) == before


def test_cli_loads_default_paths_and_prints_counts(tmp_path):
    # Run a copy of the script so its repo-root defaults point at tmp_path, not the real app.db.
    shutil.copy(REPO_ROOT / "load_seed.py", tmp_path / "load_seed.py")
    shutil.copytree(SEED_DIR, tmp_path / "seed")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "load_seed.py")], capture_output=True, text=True, check=True
    )
    assert "24 tickets" in result.stdout and "20 customers" in result.stdout
    assert rows(tmp_path / "app.db", "SELECT count(*) FROM tickets") == [(24,)]
    assert rows(tmp_path / "app.db", "SELECT count(*) FROM customers") == [(20,)]

@pytest.mark.parametrize(
    ("filename", "old", "new", "message"),
    [
        ("tickets.csv", "ticket_id,customer_id,created_at,text", "id,customer_id,created_at,text", "expected header"),
        ("customers.csv", "C-05,Hooli,Enterprise,4", "C-05,Hooli,Enterprise,many", "open_tickets must be an integer"),
    ],
    ids=["bad-header", "non-integer-open-tickets"],
)
def test_invalid_seed_changes_nothing(db, seed_copy, filename, old, new, message):
    load_seed.load_seed(db_path=db, seed_dir=SEED_DIR)
    before = db.read_bytes()
    path = seed_copy / filename
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_seed.load_seed(db_path=db, seed_dir=seed_copy)
    assert db.read_bytes() == before
