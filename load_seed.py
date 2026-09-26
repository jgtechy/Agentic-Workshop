"""Load seed/tickets.csv and seed/customers.csv into app.db (Epic 1, CAP-2).

Every run rebuilds the `tickets` and `customers` tables inside one transaction,
so a rerun gives identical contents and a failed run leaves app.db untouched.
Other tables in app.db are left alone.
"""

import csv
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = REPO_ROOT / "app.db"
DEFAULT_SEED_DIR = REPO_ROOT / "seed"

# Table name -> (CSV file, ordered (column, SQL type) pairs, primary key).
TABLES: dict[str, tuple[str, tuple[tuple[str, str], ...], str]] = {
    "tickets": (
        "tickets.csv",
        (("ticket_id", "TEXT"), ("customer_id", "TEXT"), ("created_at", "TEXT"), ("text", "TEXT")),
        "ticket_id",
    ),
    "customers": (
        "customers.csv",
        (("customer_id", "TEXT"), ("name", "TEXT"), ("plan", "TEXT"), ("open_tickets", "INTEGER")),
        "customer_id",
    ),
}


def _read_csv(path: Path, columns: tuple[tuple[str, str], ...]) -> list[tuple]:
    """Read one seed CSV, check its header and convert INTEGER columns."""
    if not path.is_file():
        raise FileNotFoundError(f"Seed file not found: {path}")
    expected = [name for name, _ in columns]
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected:
            raise ValueError(f"{path.name}: expected header {expected}, got {reader.fieldnames}")
        rows = []
        for line_no, record in enumerate(reader, start=2):
            row = []
            for name, sql_type in columns:
                value = record[name]
                if sql_type == "INTEGER":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        raise ValueError(f"{path.name} line {line_no}: {name} must be an integer, got {value!r}") from None
                row.append(value)
            rows.append(tuple(row))
    return rows


def load_seed(db_path: Path = DEFAULT_DB_PATH, seed_dir: Path = DEFAULT_SEED_DIR) -> dict[str, int]:
    """Rebuild the tickets and customers tables in db_path from seed_dir; return row counts per table."""
    db_path, seed_dir = Path(db_path), Path(seed_dir)

    # Read and validate everything before touching the database.
    data = {table: _read_csv(seed_dir / csv_name, columns) for table, (csv_name, columns, _) in TABLES.items()}

    # autocommit=False makes DROP/CREATE part of the transaction, so any failure rolls everything back.
    existed = db_path.exists()
    conn = sqlite3.connect(db_path, autocommit=False)
    try:
        for table, (_, columns, primary_key) in TABLES.items():
            column_defs = ", ".join(
                f"{name} {sql_type}{' PRIMARY KEY' if name == primary_key else ''}" for name, sql_type in columns
            )
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE TABLE {table} ({column_defs})")
            try:
                conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", data[table])
            except sqlite3.IntegrityError as error:
                raise sqlite3.IntegrityError(f"Loading table {table!r} failed: {error}") from error
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        if not existed:
            db_path.unlink(missing_ok=True)  # don't leave an empty app.db behind
        raise
    conn.close()

    return {table: len(rows) for table, rows in data.items()}


if __name__ == "__main__":
    counts = load_seed()
    print(f"Loaded {DEFAULT_DB_PATH.name}: {counts['tickets']} tickets, {counts['customers']} customers")
