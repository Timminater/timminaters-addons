"""Paired offline Timestral-123M source ablation on archived live NL forecast inputs."""
import argparse
import itertools
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from app.backend import AppService
from app.methods import timestral_forecast, timestral_version
from app.storage import Store, utc_iso
from app.timestral_calendar import weekend_covariate
from app.timestral_models import model_spec


class Archive(Store):
    def __init__(self, database):
        if Path(str(database) + "-wal").exists() and Path(str(database) + "-wal").stat().st_size:
            raise ValueError("Gebruik een afgesloten kopie zonder openstaande WAL")
        self.uri = database.resolve().as_uri() + "?mode=ro&immutable=1"

    def _connect(self):
        db = sqlite3.connect(self.uri, uri=True)
        db.row_factory = sqlite3.Row
        return db


def cases(store, remaining=False):
    settings = store.get_settings()
    spec = model_spec("123M")
    with closing(store._connect()) as db:
        last = db.execute("SELECT max(issued_at) FROM forecast_runs").fetchone()[0]
        scope = db.execute("""SELECT inputs_json,model_version FROM forecast_runs
            WHERE model_version LIKE 'timestral-local-123m-calendar-%:weather-%'
            AND coalesce(json_extract(inputs_json,'$.evaluation_mode'),'live')='live'
            ORDER BY issued_at DESC LIMIT 1""").fetchone()
    if not scope or scope["model_version"] != timestral_version(settings) or settings["weather_region"] != "europe":
        raise ValueError("De database moet dezelfde actieve 123M-variant met tienpuntsweer en weekendinformatie bevatten")
    meta = json.loads(scope["inputs_json"])
    if meta["tariff_unit"] != "EUR/kWh":
        raise ValueError("De foutberekening verwacht tarieven in EUR/kWh")
    entity = meta["tariff_entity"]
    archive = AppService._archive_entity(entity, settings)
    cutoff = datetime.fromisoformat(last)
    truth = store.published_quarters(archive, cutoff)
    runs = store.daily_stored_forecasts(entity, cutoff-timedelta(days=60), cutoff-timedelta(days=7),
                    meta["price_field"], meta["tariff_unit"], method="timestral", model_version=scope["model_version"])
    runs = runs[1:]  # First regional issue has no preceding snapshot on the same grid.
    initial = {round(i*(len(runs)-1)/6) for i in range(7)}
    for i in sorted(set(range(len(runs))) - initial if remaining else initial):
        run = runs[i]
        issue = datetime.fromisoformat(run["issued_at"])
        with closing(store._connect()) as db:
            inputs = json.loads(db.execute("SELECT inputs_json FROM forecast_runs WHERE id=?", (run["id"],)).fetchone()[0])
            store._decode_forecast_history(db, run["id"], inputs)
        weather = inputs["weather_snapshot"]
        if datetime.fromisoformat(weather["observed_at"]) > issue:
            prior = store.latest_snapshot("weather", successful_only=True, as_of=issue-timedelta(minutes=5))
            if not prior or datetime.fromisoformat(prior["payload"]["observed_at"]) > issue:
                raise ValueError("Geen tijdige weersnapshot")
            weather = prior["payload"]
            print("Tijdige eerdere weersnapshot", issue, weather["observed_at"], flush=True)
        request = {}
        def capture(method, payload, config, **kwargs):
            request.update(payload)
            values = [0.] * payload["prediction_length"]
            return dict(model=spec["repo"],revision=spec["revision"],calendar=True,weather=True,
                        lower=values,median=values,upper=values)
        forecast = timestral_forecast(inputs["history"], issue, {**settings,"_weather":weather}, runner=capture)
        known = {r["start_utc"] for r in store.quarters(archive,issue,issue+timedelta(days=7),as_of=issue)}
        common = ({p["start_utc"] for p in run["points"]} & truth.keys()) - known
        assert common <= {utc_iso(p.start_utc) for p in forecast.points}
        start = datetime.fromisoformat(request["context_start"]) + timedelta(minutes=15*len(request["context"]))
        targets = [(int((datetime.fromisoformat(t)-start).total_seconds()/900),truth[t],t) for t in sorted(common)]
        assert all(0 <= idx < request["prediction_length"] for idx,_,_ in targets)
        yield issue, request, targets


def evaluate(args):
    import numpy as np
    import torch
    from app.local_worker import verify_model
    from timestral import TimestralForecaster
    torch.set_num_threads(2)
    spec = model_spec("123M")
    folder = args.models / "models" / spec["folder"]
    verify_model(folder, spec)
    model = TimestralForecaster.from_pretrained(folder,device="cpu",token=False,local_files_only=True)
    for issue, req, targets in cases(Archive(args.database), args.remaining):
        horizon = req["prediction_length"]
        context = np.asarray(req["context"], dtype=np.float32)
        calendar = np.asarray(weekend_covariate(req["context_start"],len(context)+horizon),dtype=np.float32)[None,:,:]
        if args.grids:
            profiles = json.loads((args.grids/(issue.strftime("%Y%m%d")+".json")).read_text())
            options = [(name, data, (1,) if args.wind_only else (0,1,2), True) for name,data in profiles.items()]
        elif args.remaining:
            options = [("live-10",req["weather"],cols,True) for cols in ((1,), (0,1), (0,1,2))]
        elif args.extra_calendar:
            options = [(mode,req["weather"],(0,1,2),True) for mode in ("weekday","hour","weekday-hour")]
        else:
            options = [("live-10",req["weather"],cols,cal) for n in range(4)
                       for cols in itertools.combinations(range(3),n) for cal in (False, True)]
        for profile,weather,cols,cal in options:
            kwargs = {"categorical_known_covariates":calendar} if cal else {}
            if args.extra_calendar:
                start=datetime.fromisoformat(req["context_start"])
                local=[(start+i*timedelta(minutes=15)).astimezone(ZoneInfo("Europe/Amsterdam"))
                       for i in range(len(context)+horizon)]
                cats=[[t.weekday() for t in local]] if "weekday" in profile else [calendar[0,0]]
                if "hour" in profile:
                    cats.append([t.hour for t in local])
                kwargs["categorical_known_covariates"]=np.asarray(cats,dtype=np.float32)[None,:,:]
            if cols:
                kwargs["known_covariates"] = np.asarray([weather[i] for i in cols],dtype=np.float32)[None,:,:]
            pred = model.forecast(context,prediction_length=horizon,clip_nonnegative=False,**kwargs).median.reshape(-1)
            delta = np.asarray([float(pred[idx])-actual for idx,actual,t in targets])
            assert np.isfinite(delta).all()
            row = dict(issue=issue.isoformat(),profile=profile,cols=cols,weekend=cal,n=len(delta),
                       sum_abs_ct=float(abs(delta).sum()*100),bias_sum_ct=float(delta.sum()*100))
            with args.output.open("a",encoding="utf8") as f:
                print(json.dumps(row),file=f)
        print(issue, len(targets), flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("database","models","output"):
        parser.add_argument(name,type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--grids",type=Path)
    group.add_argument("--remaining",action="store_true",help="Check the shortlisted wind variants on remaining mature dates")
    group.add_argument("--extra-calendar",action="store_true")
    parser.add_argument("--wind-only",action="store_true",help="Use only wind in the location replay")
    args=parser.parse_args()
    if args.wind_only and not args.grids: parser.error("--wind-only requires --grids")
    if args.output.exists(): parser.error("Kies een nieuw resultaatbestand")
    evaluate(args)
