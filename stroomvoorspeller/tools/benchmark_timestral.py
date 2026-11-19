"""Compare Timestral sizes on identical saved live inputs with a cold CPU process per run (Windows)."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from statistics import mean
import subprocess
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from app.methods import timestral_forecast
from app.storage import Store, utc_iso
from app.timestral_models import MODELS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("model_directory", type=Path)
    parser.add_argument("--issues", type=int, default=7)
    parser.add_argument("--predictions", type=Path, help="Optional JSONL file for the matched predictions")
    args = parser.parse_args()
    if Path(str(args.database) + "-wal").exists() and Path(str(args.database) + "-wal").stat().st_size:
        raise ValueError("Gebruik een afgesloten databasekopie zonder openstaande WAL")
    uri = args.database.resolve().as_uri() + "?mode=ro&immutable=1"

    class ReadOnly(Store):
        def __init__(self):
            self.data_dir = args.model_directory

        def _connect(self):
            db = sqlite3.connect(uri, uri=True)
            db.row_factory = sqlite3.Row
            return db

    store = ReadOnly()
    settings = store.get_settings()
    with closing(store._connect()) as db:
        last = db.execute("SELECT max(issued_at) FROM forecast_runs").fetchone()[0]
        scope = db.execute("""SELECT inputs_json,model_version FROM forecast_runs
            WHERE json_extract(inputs_json,'$.method')='timestral'
            AND coalesce(json_extract(inputs_json,'$.evaluation_mode'),'live')='live'
            ORDER BY issued_at DESC LIMIT 1""").fetchone()
    inputs = json.loads(scope["inputs_json"])
    identity, field, unit = (inputs[k] for k in ("tariff_entity", "price_field", "tariff_unit"))
    from app.backend import AppService
    archive = AppService._archive_entity(identity, settings)
    end = datetime.fromisoformat(last.replace("Z", "+00:00"))
    runs = store.daily_stored_forecasts(identity, end-timedelta(days=60), end-timedelta(days=7),
                                         field, unit, method="timestral", model_version=scope["model_version"])
    n = min(args.issues, len(runs))
    if not n:
        raise ValueError("Geen volledig gerijpte runs")
    runs = [runs[round(i*(len(runs)-1)/max(1,n-1))] for i in range(n)]
    truth = store.published_quarters(archive, end)
    child = """import json,sys,time,psutil
from app.local_worker import infer_timestral
request=json.load(sys.stdin)
result=infer_timestral(request)
p=psutil.Process()
result['measurement']=[time.process_time(),p.memory_info().peak_wset/1048576]
print(json.dumps(result))"""
    errors, resources, predictions = defaultdict(list), defaultdict(list), defaultdict(dict)
    for run in runs:
        issue = datetime.fromisoformat(run["issued_at"].replace("Z", "+00:00"))
        with closing(store._connect()) as db:
            inputs = json.loads(db.execute("SELECT inputs_json FROM forecast_runs WHERE id=?", (run["id"],)).fetchone()[0])
            store._decode_forecast_history(db, run["id"], inputs)
        known = {r["start_utc"] for r in store.quarters(archive, issue, issue+timedelta(days=7), as_of=issue)}
        common = {p["start_utc"] for p in run["points"] if p["start_utc"] in truth} - known
        for size in MODELS:
            def worker(method, payload, config, **kwargs):
                started = time.perf_counter()
                proc = subprocess.run([sys.executable, "-c", child],
                    input=json.dumps({**payload, "model_size": size, "threads": 2}),
                    cwd=APP, env={**os.environ, "DATA_DIR": str(args.model_directory.resolve()),
                                 "HF_HUB_OFFLINE": "1", "OMP_NUM_THREADS":"2", "MKL_NUM_THREADS":"2"},
                    capture_output=True, text=True, check=True, timeout=300)
                result = json.loads(proc.stdout)
                resources[size].append([time.perf_counter()-started, *result["measurement"]])
                return result
            forecast = timestral_forecast(inputs["history"], issue, {**settings, "timestral_model": size,
                                              "_weather": inputs["weather_snapshot"]}, runner=worker)
            matched = {utc_iso(p.start_utc):p.price for p in forecast.points if utc_iso(p.start_utc) in common}
            if set(matched) != common:
                raise ValueError("Onvolledige gepaarde doelen")
            errors[size].extend(abs(price-truth[t]) for t,price in matched.items())
            for t,price in matched.items():
                predictions[run["issued_at"],t].update({size:price,"actual":truth[t]})
            print(size, run["issued_at"], len(matched), resources[size][-1], flush=True)
    for size, runs in resources.items():
        print(size, "MAE",mean(errors[size]),unit,"n",len(errors[size]),"wall avg",mean(r[0] for r in runs),
              "cpu avg",mean(r[1] for r in runs),"peak MiB",max(r[2] for r in runs))
    if args.predictions:
        with args.predictions.open("x", encoding="utf-8") as output:
            for (issue, target), values in sorted(predictions.items()):
                print(json.dumps({"issue":issue, "target":target, "unit":unit, **values}), file=output)
    for key in sorted(predictions)[::max(1,len(predictions)//4)][:4]:
        print("example",key,predictions[key])


if __name__ == "__main__":
    main()
