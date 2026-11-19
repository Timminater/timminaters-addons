"""Prepare matched 1/4/10-point GFS grids for benchmark_sources; one batch per vintage."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from benchmark_sources import Archive, cases
from app.weather_regions import locations

def fetch(cache, key, host, params):
    cache.mkdir(parents=True, exist_ok=True)
    file = cache / (key+".json")
    if file.exists():
        return json.loads(file.read_text())
    pts = locations("europe")
    query = {"latitude":",".join(str(p[1]) for p in pts),"longitude":",".join(str(p[2]) for p in pts),
             "hourly":"shortwave_radiation,wind_speed_10m,temperature_2m","models":"gfs_global",
             "timezone":"UTC","wind_speed_unit":"ms",**params}
    url="https://"+host+".open-meteo.com/v1/forecast?"+urlencode(query)
    with urlopen(url,timeout=90) as response:
        data=json.load(response)
    assert len(data)==10
    file.write_text(json.dumps(data,separators=(",",":")))
    return data


def prepare(db, dest):
    entries=list(cases(Archive(db)))
    start=min(datetime.fromisoformat(req["context_start"]) for _,req,_ in entries)
    raw=dest/"raw"
    history=fetch(raw,"context","historical-forecast-api",{"start_date":start.date().isoformat(),
                  "end_date":entries[-1][0].date().isoformat()})
    def index(grid):
        out=[]
        for p in grid:
            u=p["hourly_units"]
            assert (u["shortwave_radiation"],u["wind_speed_10m"],u["temperature_2m"])==("W/m²","m/s","°C")
            h=p["hourly"]
            out.append(dict(zip(h["time"],zip(h["shortwave_radiation"],h["wind_speed_10m"],h["temperature_2m"]))))
        return out
    past=index(history)
    for issue,req,_ in entries:
        run=issue-timedelta(hours=12)
        run=run.replace(hour=run.hour//6*6,minute=0,second=0,microsecond=0)
        vintage=index(fetch(raw,run.strftime("%Y%m%d%H"),"single-runs-api",
                     {"run":run.strftime("%Y-%m-%dT%H:%M"),"forecast_days":10}))
        total=len(req["context"])+req["prediction_length"]
        start=datetime.fromisoformat(req["context_start"])
        grids={}
        for label,indices in (("GFS-1-West",[1]),("GFS-4-NL",range(4)),("GFS-10",range(10))):
            rows=[[],[],[]]
            for j in range(total):
                t=start+j*timedelta(minutes=15)
                src=past if t<run else vintage
                hour=t.strftime("%Y-%m-%dT%H:00")
                for k in range(3):
                    v=None
                    if req["weather"][k][j] is not None:
                        all_vals=[point.get(hour,(None,None,None))[k] for point in src]
                        if all(x is not None for x in all_vals):
                            v=sum(all_vals[i] for i in indices)/len(indices)
                        elif j >= len(req["context"]):
                            raise ValueError(f"Ontbrekende voorspelling: {hour}")
                    rows[k].append(v)
            grids[label]=rows
        (dest/(issue.strftime("%Y%m%d")+".json")).write_text(json.dumps(grids,separators=(",",":")))
        print(issue,"grid ready",flush=True)

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database",type=Path)
    parser.add_argument("directory",type=Path)
    args=parser.parse_args()
    prepare(args.database,args.directory)
