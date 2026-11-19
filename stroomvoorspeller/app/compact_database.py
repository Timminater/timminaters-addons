"""Build a verified, compact copy of an existing Stroomvoorspeller database."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .storage import Store

COMPACT_VERSION = 2


def _convert_inputs(db: sqlite3.Connection, run_id: str, raw: str) -> str:
    inputs = json.loads(raw)
    weather = inputs.get("weather_snapshot")
    changed = False
    if isinstance(weather, dict):
        Store._save_weather(db, run_id, weather)
        del inputs["weather_snapshot"]
        inputs["weather_shared"] = True
        check = dict(inputs)
        Store._read_weather(db, run_id, check)
        if check["weather_snapshot"] != weather:
            raise ValueError(f"Weercontrole mislukt voor run {run_id}")
        changed = True
    if not inputs.get("history_chunked") and ({"history", "history_zlib_b64"} & inputs.keys()):
        decoded = dict(inputs)
        Store._decode_forecast_history(db, run_id, decoded)
        history = decoded.get("history")
        if history is not None:
            Store._save_forecast_history(db, run_id, history)
            inputs.pop("history", None)
            inputs.pop("history_zlib_b64", None)
            inputs["history_chunked"] = True
            check = dict(inputs)
            Store._decode_forecast_history(db, run_id, check)
            if check["history"] != history:
                raise ValueError(f"Historiecontrole mislukt voor run {run_id}")
            changed = True
    return json.dumps(inputs, separators=(",", ":"), allow_nan=False) if changed else raw


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _copy_database(source: Path, target: Path) -> int:
    Store(target.parent)
    with closing(sqlite3.connect(target.as_uri(), uri=True)) as db:
        db.row_factory = sqlite3.Row
        # This database is a disposable staging file. The original is attached read-only.
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("ATTACH DATABASE ? AS original", (source.as_uri() + "?mode=ro",))
        with db:
            db.execute("BEGIN")
            source_version = db.execute("PRAGMA original.user_version").fetchone()[0]
            source_tables = {row[0] for row in db.execute(
                "SELECT name FROM original.sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            target_tables = {row[0] for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table'")}
            if not {"forecast_runs", "forecast_points"} <= source_tables or source_tables - target_tables:
                raise ValueError("De bron heeft een onbekende Stroomvoorspeller-databasestructuur")
            for name in sorted(source_tables):
                table = _quoted(name)
                columns = [row["name"] for row in db.execute(f"PRAGMA original.table_info({table})")]
                writable = {row["name"] for row in db.execute(f"PRAGMA main.table_info({table})")}
                if set(columns) - writable:
                    raise ValueError(f"De kopie ondersteunt niet alle velden in {name}")
                if name in {"forecast_runs", "forecast_points"}:
                    if set(columns) != writable:
                        raise ValueError(f"De bron heeft een andere indeling voor {name}")
                    continue
                fields = ", ".join(map(_quoted, columns))
                db.execute(f"INSERT INTO main.{table} ({fields}) SELECT {fields} FROM original.{table}")
            if db.execute("SELECT 1 FROM original.sqlite_master WHERE name='sqlite_sequence'").fetchone():
                db.execute("DELETE FROM main.sqlite_sequence")
                db.execute("INSERT INTO main.sqlite_sequence SELECT * FROM original.sqlite_sequence")

            converted = 0
            for run in db.execute("""
                SELECT id,issued_at,model_version,quality,reasons_json,inputs_json
                FROM original.forecast_runs ORDER BY rowid
            """):
                raw = _convert_inputs(db, run["id"], run["inputs_json"])
                db.execute("INSERT INTO main.forecast_runs VALUES(?,?,?,?,?,?)", (*tuple(run)[:5], raw))
                converted += raw != run["inputs_json"]
            db.execute("""
                INSERT INTO main.forecast_points
                SELECT run_id,start_utc,end_utc,price,lower_price,upper_price,source,quality,reason
                FROM original.forecast_points ORDER BY run_id,start_utc
            """)
            converted_tables = {"forecast_history_chunks", "forecast_history_refs", "weather_blobs", "forecast_weather_refs"}
            for name in sorted(source_tables - converted_tables):
                table = _quoted(name)
                before = db.execute(f"SELECT count(*) FROM original.{table}").fetchone()[0]
                after = db.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]
                if before != after:
                    raise ValueError(f"Onverwacht aantal records in {name}")
            if db.execute("""
                SELECT 1 FROM original.forecast_points o LEFT JOIN main.forecast_points n USING(run_id,start_utc)
                WHERE n.run_id IS NULL OR n.end_utc IS NOT o.end_utc OR n.price IS NOT o.price
                   OR n.lower_price IS NOT o.lower_price OR n.upper_price IS NOT o.upper_price
                   OR n.source IS NOT o.source OR n.quality IS NOT o.quality OR n.reason IS NOT o.reason LIMIT 1
            """).fetchone():
                raise ValueError("Controle van gekopieerde voorspelpunten mislukt")
            db.execute(f"PRAGMA main.user_version={max(COMPACT_VERSION, source_version)}")

        db.execute("DETACH DATABASE original")
        db.execute("PRAGMA journal_mode=DELETE")
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("De compacte database is niet intact")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("De compacte database bevat ongeldige verwijzingen")
    return converted


def compact_copy(source: Path, output_dir: Path) -> tuple[Path, int]:
    """Read a consistent snapshot, including committed WAL, into one staging file."""
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Kies een bestaand SQLite-databasebestand")
    output_dir.mkdir()
    output = output_dir / "stroomvoorspeller.sqlite3"
    with TemporaryDirectory(dir=output_dir) as temp:
        target = Path(temp) / output.name
        converted = _copy_database(source, target)
        target.rename(output)
    return output, converted


def _needs_conversion(db: sqlite3.Connection) -> bool:
    if db.execute("PRAGMA user_version").fetchone()[0] >= COMPACT_VERSION:
        return False
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='forecast_points'").fetchone()
    if schema is None:
        raise ValueError("De productiedatabase bevat geen tabel met voorspelpunten")
    return ("WITHOUT ROWID" not in schema[0].upper() or db.execute("""
        SELECT 1 FROM forecast_runs
         WHERE json_type(inputs_json,'$.weather_snapshot')='object'
            OR (coalesce(json_extract(inputs_json,'$.history_chunked'),0)=0
                AND (json_type(inputs_json,'$.history')='array'
                     OR json_type(inputs_json,'$.history_zlib_b64')='text'))
         LIMIT 1
    """).fetchone() is not None)


def _clear_staging(staging: Path) -> None:
    if staging.is_symlink():
        raise ValueError("De tijdelijke migratiemap mag geen symlink zijn")
    if not staging.exists():
        return
    for suffix in ("-wal", "-shm", "-journal", ""):
        (staging / ("stroomvoorspeller.sqlite3" + suffix)).unlink(missing_ok=True)
    staging.rmdir()


def compact_on_start(source: Path) -> tuple[int, int, int] | None:
    """Replace the stopped app's database only after building and checking a copy."""
    import fcntl

    source = source.resolve(strict=True)
    lock_path = source.parent / ".stroomvoorspeller-compaction.lock"
    staging = source.parent / ".stroomvoorspeller-compaction"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _clear_staging(staging)
        with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=5)) as original:
            if not _needs_conversion(original):
                if original.execute("PRAGMA user_version").fetchone()[0] < COMPACT_VERSION:
                    original.execute(f"PRAGMA user_version={COMPACT_VERSION}")
                return None
            before = source.stat().st_size
            if shutil.disk_usage(source.parent).free < before + 256 * 1024 ** 2:
                raise ValueError("Onvoldoende vrije schijfruimte voor de automatische databaseomzetting")
            if original.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("De oorspronkelijke database is nog in gebruik")
            revision = original.execute("PRAGMA data_version").fetchone()[0]
            staging.mkdir(mode=0o700)
            try:
                candidate = staging / "stroomvoorspeller.sqlite3"
                count = _copy_database(source, candidate)
                candidate.chmod(source.stat().st_mode & 0o777)
                with candidate.open("rb") as persisted:
                    os.fsync(persisted.fileno())
                after = candidate.stat().st_size
                original.execute("BEGIN EXCLUSIVE")
                if (original.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
                        or original.execute("PRAGMA data_version").fetchone()[0] != revision):
                    raise ValueError("De oorspronkelijke database is tijdens het kopiëren gewijzigd")
                os.replace(candidate, source)
                directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                return before, after, count
            finally:
                _clear_staging(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Bestaande SQLite-database")
    parser.add_argument("output_dir", type=Path, help="Nieuwe map voor de gecontroleerde kopie")
    args = parser.parse_args()
    output, converted = compact_copy(args.source, args.output_dir.resolve())
    print(f"{converted} runs omgezet. Compacte kopie: {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
