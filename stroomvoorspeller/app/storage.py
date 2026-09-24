"""Local durable storage for the Stroomvoorspeller Home Assistant App.

All timestamps are stored as UTC ISO-8601 strings with an explicit ``Z``. A
price row always identifies one 15 minute interval and its originating tariff
entity. Hourly history is stored separately and can never be mistaken for a
measured quarter.
"""
from __future__ import annotations

import json
import base64
import os
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

UTC = timezone.utc
RETENTION_DAYS = 180


def price_archive_key(entity_id: str, price_field: str = "tax_included",
                      tariff_unit: str | None = None) -> str:
    """Return the durable namespace for one entity/field/unit price series."""
    return f"{entity_id}|price_field={price_field}|unit={tariff_unit or 'unspecified'}"


def utc_iso(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    """SQLite repository. Production callers pass ``/data`` via DATA_DIR."""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", "/data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "stroomvoorspeller.sqlite3"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quarter_prices (
              entity_id TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
              price REAL NOT NULL, unit TEXT NOT NULL, source TEXT NOT NULL,
              observed_at TEXT NOT NULL, published_at TEXT, quality TEXT NOT NULL DEFAULT 'valid',
              PRIMARY KEY(entity_id, start_utc), CHECK(end_utc > start_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_prices_start ON quarter_prices(start_utc);
            CREATE TABLE IF NOT EXISTS quarter_price_revisions (
              entity_id TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
              price REAL NOT NULL, unit TEXT NOT NULL, source TEXT NOT NULL,
              observed_at TEXT NOT NULL, published_at TEXT, quality TEXT NOT NULL DEFAULT 'valid',
              PRIMARY KEY(entity_id,start_utc,observed_at), CHECK(end_utc > start_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_price_revisions_asof ON quarter_price_revisions(entity_id,observed_at,start_utc);
            CREATE TABLE IF NOT EXISTS hourly_history (
              entity_id TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
              price REAL NOT NULL, unit TEXT NOT NULL, source TEXT NOT NULL,
              observed_at TEXT NOT NULL, PRIMARY KEY(entity_id, start_utc)
            );
            CREATE TABLE IF NOT EXISTS input_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT, issued_at TEXT NOT NULL,
              source TEXT NOT NULL, payload_json TEXT NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_issued ON input_snapshots(issued_at);
            CREATE TABLE IF NOT EXISTS forecast_runs (
              id TEXT PRIMARY KEY, issued_at TEXT NOT NULL, model_version TEXT NOT NULL,
              quality TEXT NOT NULL, reasons_json TEXT NOT NULL, inputs_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_issued ON forecast_runs(issued_at);
            CREATE TABLE IF NOT EXISTS forecast_points (
              run_id TEXT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
              start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, price REAL,
              lower_price REAL, upper_price REAL, source TEXT NOT NULL,
              quality TEXT NOT NULL, reason TEXT, PRIMARY KEY(run_id, start_utc)
            );
            CREATE TABLE IF NOT EXISTS source_status (
              source TEXT PRIMARY KEY, last_success TEXT, last_attempt TEXT,
              error TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
            );
            PRAGMA user_version=1;
            """)

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key,value_json FROM settings").fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def set_settings(self, values: Mapping[str, Any]) -> dict[str, Any]:
        now = utc_iso(datetime.now(UTC))
        with self._lock, self._connect() as db:
            for key, value in values.items():
                db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                           (key, json.dumps(value, separators=(",", ":"), allow_nan=False), now))
        return self.get_settings()

    def upsert_quarters(self, rows: Iterable[Mapping[str, Any]]) -> int:
        items = []
        for r in rows:
            start, end = utc_iso(r["start_utc"]), utc_iso(r["end_utc"])
            if datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00")) != timedelta(minutes=15):
                raise ValueError("quarter interval must be exactly 15 minutes")
            price = float(r["price"])
            if not (-100_000 <= price <= 100_000):
                raise ValueError("price outside accepted numeric bounds")
            items.append((str(r["entity_id"]), start, end, price, str(r.get("unit", "")),
                          str(r.get("source", "forecast")), utc_iso(r["observed_at"]),
                          utc_iso(r["published_at"]) if r.get("published_at") else None,
                          str(r.get("quality", "valid"))))
        with self._lock, self._connect() as db:
            for item in items:
                entity_id, start, end, price, unit, source, observed, published, quality = item
                old = db.execute("SELECT * FROM quarter_prices WHERE entity_id=? AND start_utc=?", (entity_id,start)).fetchone()
                # Repeated polls of an unchanged published price do not move its
                # first-seen/publication time forward. A changed value receives
                # a new immutable point-in-time revision.
                if old and old["price"] == price and old["unit"] == unit and old["end_utc"] == end:
                    published = old["published_at"]
                    if old["source"] == source and old["quality"] == quality:
                        continue
                revision = (entity_id,start,end,price,unit,source,observed,published,quality)
                db.execute("INSERT OR IGNORE INTO quarter_price_revisions VALUES(?,?,?,?,?,?,?,?,?)", revision)
                db.execute("""INSERT INTO quarter_prices(entity_id,start_utc,end_utc,price,unit,source,observed_at,published_at,quality)
                  VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id,start_utc) DO UPDATE SET
                  end_utc=excluded.end_utc,price=excluded.price,unit=excluded.unit,source=excluded.source,
                  observed_at=excluded.observed_at,published_at=excluded.published_at,quality=excluded.quality""", revision)
        return len(items)

    def upsert_hourly(self, rows: Iterable[Mapping[str, Any]]) -> int:
        items = [(str(r["entity_id"]), utc_iso(r["start_utc"]), utc_iso(r["end_utc"]), float(r["price"]),
                  str(r.get("unit", "")), str(r.get("source", "history_api")), utc_iso(r["observed_at"])) for r in rows]
        with self._lock, self._connect() as db:
            db.executemany("""INSERT INTO hourly_history VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_id,start_utc)
              DO UPDATE SET end_utc=excluded.end_utc,price=excluded.price,unit=excluded.unit,
              source=excluded.source,observed_at=excluded.observed_at""", items)
        return len(items)

    def quarters(self, entity_id: str, start: datetime | None = None, end: datetime | None = None,
                 *, as_of: datetime | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quarter_prices WHERE entity_id=?"
        args: list[Any] = [entity_id]
        if start:
            sql += " AND start_utc>=?"; args.append(utc_iso(start))
        if end:
            sql += " AND start_utc<?"; args.append(utc_iso(end))
        if as_of:
            # Immutable revisions make as-of reads stable after later updates.
            with self._connect() as db:
                rows = db.execute("""SELECT r.* FROM quarter_price_revisions r
                    JOIN (SELECT entity_id,start_utc,MAX(observed_at) observed_at
                          FROM quarter_price_revisions WHERE entity_id=? AND observed_at<=?
                          GROUP BY entity_id,start_utc) latest
                    ON latest.entity_id=r.entity_id AND latest.start_utc=r.start_utc AND latest.observed_at=r.observed_at
                    WHERE r.entity_id=? AND r.start_utc>=COALESCE(?,r.start_utc)
                      AND r.start_utc<COALESCE(?, '9999-12-31T00:00:00Z')
                      AND (r.published_at IS NULL OR r.published_at<=?) ORDER BY r.start_utc""",
                    (entity_id,utc_iso(as_of),entity_id,utc_iso(start) if start else None,
                     utc_iso(end) if end else None,utc_iso(as_of))).fetchall()
            return [dict(r) for r in rows]
        sql += " ORDER BY start_utc"
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def hourly(self, entity_id: str, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM hourly_history WHERE entity_id=?"; args: list[Any] = [entity_id]
        if start: sql += " AND start_utc>=?"; args.append(utc_iso(start))
        if end: sql += " AND start_utc<?"; args.append(utc_iso(end))
        sql += " ORDER BY start_utc"
        with self._connect() as db: rows = db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def save_snapshot(self, issued_at: datetime, source: str, payload: Any, error: str | None = None) -> int:
        with self._lock, self._connect() as db:
            cur = db.execute("INSERT INTO input_snapshots(issued_at,source,payload_json,error) VALUES(?,?,?,?)",
                             (utc_iso(issued_at), source, json.dumps(payload, separators=(",", ":"), allow_nan=False), error))
            return int(cur.lastrowid)

    def latest_snapshot(self, source: str, successful_only: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            sql = "SELECT * FROM input_snapshots WHERE source=?"
            if successful_only: sql += " AND error IS NULL"
            sql += " ORDER BY issued_at DESC,id DESC LIMIT 1"
            row = db.execute(sql, (source,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def save_forecast(self, run_id: str, issued_at: datetime, model_version: str, quality: str,
                      reasons: list[str], inputs: Mapping[str, Any], points: Iterable[Mapping[str, Any]]) -> None:
        compact_inputs = dict(inputs)
        history = compact_inputs.pop("history", None)
        if history is not None:
            packed = json.dumps(history, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
            compact_inputs["history_zlib_b64"] = base64.b64encode(zlib.compress(packed, level=6)).decode("ascii")
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO forecast_runs VALUES(?,?,?,?,?,?)",
                       (run_id, utc_iso(issued_at), model_version, quality,
                        json.dumps(reasons, separators=(",", ":")),
                        json.dumps(compact_inputs, separators=(",", ":"), allow_nan=False, default=str)))
            db.execute("DELETE FROM forecast_points WHERE run_id=?", (run_id,))
            db.executemany("INSERT INTO forecast_points VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, utc_iso(p["start_utc"]), utc_iso(p["end_utc"]), p.get("price"), p.get("lower"), p.get("upper"),
                  str(p.get("source", "model")), str(p.get("quality", quality)), p.get("reason")) for p in points])

    def latest_forecast(self, entity_id: str | None = None, price_field: str | None = None,
                        tariff_unit: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with self._connect() as db:
            runs = db.execute("SELECT * FROM forecast_runs ORDER BY issued_at DESC").fetchall()
            run = None
            for candidate in runs:
                try: inputs = json.loads(candidate["inputs_json"])
                except (ValueError, TypeError): continue
                if ((entity_id is None or inputs.get("tariff_entity") == entity_id)
                        and (price_field is None or inputs.get("price_field", "tax_included") == price_field)
                        and (tariff_unit is None or inputs.get("tariff_unit") == tariff_unit)):
                    run = candidate; break
            if not run: return None, []
            points = db.execute("SELECT * FROM forecast_points WHERE run_id=? ORDER BY start_utc", (run["id"],)).fetchall()
        d = dict(run); d["reasons"] = json.loads(d.pop("reasons_json")); d["inputs"] = json.loads(d.pop("inputs_json"))
        compressed = d["inputs"].pop("history_zlib_b64", None)
        if compressed:
            d["inputs"]["history"] = json.loads(zlib.decompress(base64.b64decode(compressed)))
        return d, [dict(p) for p in points]

    def forecast_snapshots(self, entity_id: str | None = None,
                           since_issued: datetime | None = None,
                           price_field: str | None = None,
                           tariff_unit: str | None = None) -> list[dict[str, Any]]:
        """Return retained point-in-time input snapshots for walk-forward evaluation."""
        with self._connect() as db:
            if since_issued:
                runs = db.execute("SELECT * FROM forecast_runs WHERE issued_at>=? ORDER BY issued_at",
                                  (utc_iso(since_issued),)).fetchall()
            else:
                runs = db.execute("SELECT * FROM forecast_runs ORDER BY issued_at").fetchall()
        result = []
        for row in runs:
            try:
                inputs = json.loads(row["inputs_json"])
                if entity_id is not None and inputs.get("tariff_entity") != entity_id: continue
                if price_field is not None and inputs.get("price_field", "tax_included") != price_field: continue
                if tariff_unit is not None and inputs.get("tariff_unit") != tariff_unit: continue
                compressed = inputs.pop("history_zlib_b64", None)
                if compressed: inputs["history"] = json.loads(zlib.decompress(base64.b64decode(compressed)))
                # Backtests must replay exactly the normalized weather values
                # passed to the model, not the raw provider snapshot.
                if "weather_model" in inputs:
                    inputs["weather"] = inputs.get("weather_model")
                elif "weather_snapshot" in inputs:
                    inputs["weather"] = inputs.get("weather_snapshot")
                result.append({"id": row["id"], "issued_at": row["issued_at"], "quality": row["quality"],
                               "model_version": row["model_version"], "inputs": inputs, **inputs})
            except (ValueError, zlib.error):
                continue
        return result

    def stored_forecasts(self, entity_id: str, since_issued: datetime,
                         price_field: str | None = None,
                         tariff_unit: str | None = None) -> list[dict[str, Any]]:
        """Point-in-time snapshots paired with the exact predictions saved for each run."""
        snapshots = {s["id"]: s for s in self.forecast_snapshots(entity_id, since_issued, price_field, tariff_unit)}
        if not snapshots: return []
        with self._connect() as db:
            rows = db.execute("""SELECT r.id,r.issued_at,p.start_utc,p.end_utc,p.price,p.lower_price,p.upper_price
              FROM forecast_runs r JOIN forecast_points p ON p.run_id=r.id
              WHERE r.issued_at>=? ORDER BY r.issued_at,p.start_utc""", (utc_iso(since_issued),)).fetchall()
        by_issue: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["id"] in snapshots:
                by_issue.setdefault(row["id"], []).append(dict(row))
        return [{**snapshots[run_id], "points": by_issue.get(run_id, [])}
                for run_id in sorted(snapshots, key=lambda key: snapshots[key]["issued_at"])]

    def daily_stored_forecasts(self, entity_id: str, since_issued: datetime, ended_by: datetime,
                               price_field: str, tariff_unit: str,
                               timezone_name: str = "Europe/Amsterdam") -> list[dict[str, Any]]:
        """Return one point-in-time forecast per local day without inflating all snapshots.

        Run selection needs only issue timestamps and the three namespace
        fields. The compressed model history is deliberately not decoded.
        Selected runs' points are then fetched in one bounded query.
        """
        zone = ZoneInfo(timezone_name)
        with self._connect() as db:
            candidates = db.execute("""SELECT id,issued_at FROM forecast_runs
                WHERE issued_at>=? AND issued_at<=?
                  AND json_extract(inputs_json,'$.tariff_entity')=?
                  AND COALESCE(json_extract(inputs_json,'$.price_field'),'tax_included')=?
                  AND json_extract(inputs_json,'$.tariff_unit')=?
                ORDER BY issued_at""",
                (utc_iso(since_issued), utc_iso(ended_by), entity_id, price_field, tariff_unit)).fetchall()
            by_day: dict[Any, list[Any]] = {}
            for row in candidates:
                issued = datetime.fromisoformat(row["issued_at"].replace("Z", "+00:00"))
                by_day.setdefault(issued.astimezone(zone).date(), []).append(row)
            selected = []
            for day, rows in sorted(by_day.items()):
                noon = datetime.combine(day, datetime.min.time(), zone) + timedelta(hours=12)
                selected.append(min(rows, key=lambda row: (
                    abs((datetime.fromisoformat(row["issued_at"].replace("Z", "+00:00"))
                         - noon.astimezone(UTC)).total_seconds()), row["issued_at"])))
            if not selected:
                return []
            ids = [row["id"] for row in selected]
            placeholders = ",".join("?" for _ in ids)
            point_rows = db.execute(f"""SELECT run_id,start_utc,end_utc,price,lower_price,upper_price
                FROM forecast_points WHERE run_id IN ({placeholders}) ORDER BY start_utc""", ids).fetchall()
        points_by_run: dict[str, list[dict[str, Any]]] = {}
        for row in point_rows:
            point = dict(row)
            point["lower"] = point.pop("lower_price")
            point["upper"] = point.pop("upper_price")
            points_by_run.setdefault(row["run_id"], []).append(point)
        return [{"id": row["id"], "issued_at": row["issued_at"],
                 "points": points_by_run.get(row["id"], [])} for row in selected]

    def actual_quarters(self, entity_id: str, ended_by: datetime) -> dict[str, float]:
        """Final locally archived quarter prices whose complete interval ended by time."""
        end = utc_iso(ended_by)
        with self._connect() as db:
            rows = db.execute("SELECT start_utc,end_utc,price FROM quarter_prices "
                              "WHERE entity_id=? AND end_utc<=? AND quality='valid' ORDER BY start_utc", (entity_id,end)).fetchall()
        return {row["start_utc"]: float(row["price"]) for row in rows}

    def published_quarters(self, entity_id: str, known_by: datetime) -> dict[str, float]:
        """Published tariffs known by this time, including future price intervals.

        Day-ahead tariffs can be compared with an earlier forecast as soon as
        Home Assistant publishes them; consumption of the quarter is irrelevant.
        """
        cutoff = utc_iso(known_by)
        with self._connect() as db:
            rows = db.execute("SELECT start_utc,price FROM quarter_prices "
                              "WHERE entity_id=? AND quality='valid' AND observed_at<=? "
                              "AND (published_at IS NULL OR published_at<=?) ORDER BY start_utc",
                              (entity_id, cutoff, cutoff)).fetchall()
        return {row["start_utc"]: float(row["price"]) for row in rows}

    def set_source_status(self, source: str, *, success: bool, attempted_at: datetime,
                          error: str | None = None, detail: Mapping[str, Any] | None = None) -> None:
        now = utc_iso(attempted_at)
        with self._lock, self._connect() as db:
            previous = db.execute("SELECT last_success FROM source_status WHERE source=?", (source,)).fetchone()
            last_success = now if success else (previous[0] if previous else None)
            db.execute("INSERT INTO source_status VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                       "last_success=excluded.last_success,last_attempt=excluded.last_attempt,error=excluded.error,detail_json=excluded.detail_json",
                       (source, last_success, now, error, json.dumps(detail or {}, separators=(",", ":"))))

    def source_statuses(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db: rows = db.execute("SELECT * FROM source_status").fetchall()
        return {r["source"]: {**dict(r), "detail": json.loads(r["detail_json"])} for r in rows}

    def archive_summary(self, entity_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) n,MIN(start_utc) first,MAX(end_utc) last FROM quarter_prices WHERE entity_id=? AND quality='valid'", (entity_id,)).fetchone()
        expected = 0
        ratio = 0.0
        if row["first"] and row["last"]:
            seconds = (datetime.fromisoformat(row["last"].replace("Z", "+00:00")) - datetime.fromisoformat(row["first"].replace("Z", "+00:00"))).total_seconds()
            expected = max(1, int(round(seconds / 900)))
            ratio = min(1.0, row["n"] / expected)
        return {"quarter_count": row["n"], "first_start": row["first"], "last_end": row["last"],
                "coverage_days": round((datetime.fromisoformat(row["last"].replace("Z", "+00:00")) - datetime.fromisoformat(row["first"].replace("Z", "+00:00"))).total_seconds()/86400, 2) if row["first"] and row["last"] else 0,
                "expected_quarters": expected, "coverage_ratio": round(ratio, 4), "coverage_95": ratio >= 0.95}

    def prune(self, now: datetime | None = None) -> int:
        cutoff = utc_iso((now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS))
        with self._lock, self._connect() as db:
            a = db.execute("DELETE FROM input_snapshots WHERE issued_at<?", (cutoff,)).rowcount
            db.execute("DELETE FROM forecast_runs WHERE issued_at<?", (cutoff,))
            # Price observations are the durable archive and are intentionally never pruned.
        return a

    def known_and_latest_forecast(self, entity_id: str, start: datetime, end: datetime,
                                  price_field: str | None = None,
                                  tariff_unit: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        known = self.quarters(entity_id, start, end)
        run, points = self.latest_forecast(entity_id, price_field, tariff_unit)
        if run:
            lower, upper = utc_iso(start), utc_iso(end)
            points = [p for p in points if lower <= p["start_utc"] < upper]
        return known, points
