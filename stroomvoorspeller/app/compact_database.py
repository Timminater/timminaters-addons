"""Build a verified, compact copy of an existing Stroomvoorspeller database."""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .storage import Store


def _convert_history(store: Store) -> int:
    converted = 0
    cursor = None
    with closing(store._connect()) as db:
        while True:
            if cursor is None:
                batch = db.execute("SELECT rowid,id,inputs_json FROM forecast_runs ORDER BY rowid LIMIT 64").fetchall()
            else:
                batch = db.execute(
                    "SELECT rowid,id,inputs_json FROM forecast_runs WHERE rowid>? ORDER BY rowid LIMIT 64",
                    (cursor,),
                ).fetchall()
            if not batch:
                return converted
            with db:
                for run in batch:
                    inputs = json.loads(run["inputs_json"])
                    weather = inputs.get("weather_snapshot")
                    changed = False
                    if isinstance(weather, dict):
                        store._save_weather(db, run["id"], weather)
                        del inputs["weather_snapshot"]
                        inputs["weather_shared"] = True
                        check = dict(inputs)
                        store._read_weather(db, run["id"], check)
                        if check["weather_snapshot"] != weather:
                            raise ValueError(f"Weercontrole mislukt voor run {run['id']}")
                        changed = True
                    if not inputs.get("history_chunked") and ({"history", "history_zlib_b64"} & inputs.keys()):
                        decoded = dict(inputs)
                        store._decode_forecast_history(db, run["id"], decoded)
                        history = decoded.get("history")
                        if history is not None:
                            store._save_forecast_history(db, run["id"], history)
                            inputs.pop("history", None)
                            inputs.pop("history_zlib_b64", None)
                            inputs["history_chunked"] = True
                            check = dict(inputs)
                            store._decode_forecast_history(db, run["id"], check)
                            if check["history"] != history:
                                raise ValueError(f"Historiecontrole mislukt voor run {run['id']}")
                            changed = True
                    if not changed:
                        continue
                    db.execute("UPDATE forecast_runs SET inputs_json=? WHERE id=?",
                               (json.dumps(inputs, separators=(",", ":"), allow_nan=False), run["id"]))
                    converted += 1
            cursor = batch[-1]["rowid"]


def _compact_points(store):
    with closing(store._connect()) as db, db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='forecast_points'").fetchone()[0]
        if "WITHOUT ROWID" in schema.upper():
            return
        db.execute("BEGIN")
        db.execute(schema.replace("forecast_points", "compact_points", 1) + " WITHOUT ROWID")
        db.execute("INSERT INTO compact_points SELECT * FROM forecast_points ORDER BY run_id,start_utc")
        old_count = db.execute("SELECT count(*) FROM forecast_points").fetchone()[0]
        new_count = db.execute("SELECT count(*) FROM compact_points").fetchone()[0]
        if old_count != new_count or db.execute("""
            SELECT 1 FROM forecast_points o LEFT JOIN compact_points n USING(run_id,start_utc)
            WHERE n.run_id IS NULL OR n.end_utc IS NOT o.end_utc OR n.price IS NOT o.price
               OR n.lower_price IS NOT o.lower_price OR n.upper_price IS NOT o.upper_price
               OR n.source IS NOT o.source OR n.quality IS NOT o.quality OR n.reason IS NOT o.reason LIMIT 1
        """).fetchone():
            raise ValueError("Controle van gekopieerde voorspelpunten mislukt")
        db.execute("DROP TABLE forecast_points")
        db.execute("ALTER TABLE compact_points RENAME TO forecast_points")


def compact_copy(source: Path, output_dir: Path) -> tuple[Path, int]:
    """Copy committed data, including WAL, and publish only after verification."""
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Kies een bestaand SQLite-databasebestand")
    output_dir.mkdir()
    output = output_dir / "stroomvoorspeller.sqlite3"
    with TemporaryDirectory(dir=output_dir) as temp:
        work = Path(temp) / output.name
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            # Reject unrelated SQLite files before Store initializes any tables.
            original.execute("SELECT id,inputs_json FROM forecast_runs LIMIT 0")
            with closing(sqlite3.connect(work)) as working:
                original.backup(working)
        store = Store(temp)
        converted = _convert_history(store)
        _compact_points(store)
        compact = Path(temp) / "compact.sqlite3"
        with closing(store._connect()) as db:
            db.execute("VACUUM INTO ?", (str(compact),))
        with closing(sqlite3.connect(compact.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("De compacte database is niet intact")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("De compacte database bevat ongeldige verwijzingen")
        compact.rename(output)
    return output, converted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Bestaande SQLite-database")
    parser.add_argument("output_dir", type=Path, help="Nieuwe map voor de gecontroleerde kopie")
    args = parser.parse_args()
    output, converted = compact_copy(args.source, args.output_dir.resolve())
    print(f"{converted} runs omgezet. Compacte kopie: {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
