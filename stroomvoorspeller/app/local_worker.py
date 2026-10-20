"""Private CLI child for bundled ML. No HTTP server and no arbitrary commands."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys

from .local_runtime import MODEL, REVISION, WEIGHTS_SHA256, WEIGHTS_BYTES

CONFIG_SHA256 = "1764139bb6d37ee5ff543f470495c086cfa6c373f7134fc61c7e3d844538bfd8"
# Timestral's official package uses absolute 'timestral' imports internally.
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))


def model_dir():
    return Path(os.environ["DATA_DIR"]) / "models" / "timestral-209m"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_model(folder):
    try:
        valid = ((folder / "LICENSE").is_file()
                 and (folder / "model.safetensors").stat().st_size == WEIGHTS_BYTES
                 and digest(folder / "model.safetensors") == WEIGHTS_SHA256
                 and digest(folder / "config.json") == CONFIG_SHA256)
    except OSError:
        valid = False
    if not valid:
        (folder / "verified.json").unlink(missing_ok=True)
        raise ValueError("Timestral-bestanden ontbreken of SHA-256 klopt niet; download het model opnieuw")


def download_timestral(payload):
    from huggingface_hub import snapshot_download
    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True)
    snapshot_download(MODEL, revision=REVISION, token=False, local_dir=folder,
                      allow_patterns=["config.json", "model.safetensors", "LICENSE"], max_workers=1)
    verify_model(folder)
    marker = folder / "verified.json"
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"revision": REVISION, "sha256": WEIGHTS_SHA256}))
    temporary.replace(marker)
    return {"ready": True, "revision": REVISION}


def validate_timestral(payload):
    context, horizon = payload.get("context"), payload.get("prediction_length")
    if (not isinstance(context, list) or not 192 <= len(context) <= 8640
            or type(horizon) is not int or not 1 <= horizon <= 672):
        raise ValueError("Timestral verwacht 192–8640 historische en 1–672 toekomstige kwartieren")
    if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in context):
        raise ValueError("Timestral-prijzen moeten eindig zijn; gebruik null voor ontbrekende waarden")
    if sum(v is not None for v in context) < 192:
        raise ValueError("Minstens 192 echte kwartieren nodig")
    return context, horizon


def infer_timestral(payload):
    context, horizon = validate_timestral(payload)
    folder = model_dir()
    if not (folder / "verified.json").is_file():
        raise ValueError("Download en bevestig Timestral eerst in Instellingen")
    verify_model(folder)
    import numpy as np
    import torch
    from timestral import TimestralForecaster
    torch.set_num_threads(payload["threads"])
    # A local folder + local_files_only + offline environment: NEVER download here.
    model = TimestralForecaster.from_pretrained(folder, device="cpu", token=False, local_files_only=True)
    forecast = model.forecast(np.asarray([np.nan if v is None else v for v in context], dtype=np.float32),
                              prediction_length=horizon, clip_nonnegative=False)
    return {"model": MODEL, "revision": REVISION,
            "median": forecast.median.reshape(-1).tolist(),
            "lower": forecast.quantile(.1).reshape(-1).tolist(),
            "upper": forecast.quantile(.9).reshape(-1).tolist()}


async def infer_epex(payload):
    from datetime import datetime, timedelta, timezone
    import lightgbm as lgb
    from .vendor.epexpredictor.pricepredictor import PricePredictor
    from .vendor.epexpredictor.priceregion import PriceRegionName
    issue = datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00"))
    days, count = payload.get("training_days", 180), payload.get("max_points", 672)
    if (issue.tzinfo is None or type(days) is not int or not 30 <= days <= 180
            or type(count) is not int or not 1 <= count <= 672):
        raise ValueError("Ongeldige Epex-trainingsperiode of horizon")
    issue = issue.astimezone(timezone.utc)
    start = issue.replace(minute=issue.minute // 15 * 15, second=0, microsecond=0)
    end = start + timedelta(minutes=15 * count)
    folder = Path(os.environ["DATA_DIR"]) / "models" / "epex"
    folder.mkdir(parents=True, exist_ok=True)
    predictor = PricePredictor(PriceRegionName.NL.to_region(), str(folder))
    await predictor.load_from_persistence()
    # Even a cache containing published day-ahead prices may not train on targets.
    predictor.pricestore.horizon_cutoff = start - timedelta(minutes=15)
    train_start, train_end = start - timedelta(days=days), start - timedelta(minutes=15)
    # Request authoritative raw weather and market prices, never an external forecast.
    frame = await predictor.prepare_dataframe(train_start, train_end)
    if frame is None:
        raise ValueError("Geen Epex-trainingsgegevens beschikbaar")
    training = frame.dropna()
    if len(training) < 14 * 96:
        raise ValueError("Epex heeft minstens 14 dagen complete markt-/weerkwartieren nodig")
    features = training.drop(columns=["price"])
    predictor.predictor = lgb.train({"force_col_wise": True, "verbosity": -1,
        "num_threads": payload["threads"], "seed": 0}, lgb.Dataset(features, label=training["price"]))
    # Save only one current model; future calls still rebuild using bounded inputs.
    predictor.predictor.save_model(str(folder / "NL-model.txt"))
    # Reuse a complete recent forecast cache. New/missing target quarters or
    # an expired cache trigger a source refresh, never an external model call.
    import pandas as pd
    expected = pd.date_range(start=start, end=end - timedelta(minutes=15), freq="15min")
    cached_weather = predictor.weatherstore.data.reindex(expected)
    complete = len(cached_weather.columns) > 0 and bool(cached_weather.notna().all(axis=1).all())
    weather_file = Path(predictor.weatherstore.get_storage_file())
    age = (datetime.now(timezone.utc).timestamp() - weather_file.stat().st_mtime
           if weather_file.exists() else float("inf"))
    weather_cached = complete and age < 3600
    if not weather_cached:
        try:
            await predictor.weatherstore.refresh_range(start, end)
        except (OSError, TimeoutError):
            if not complete or age > 6 * 3600:
                raise ValueError("Epex-weergegevens niet beschikbaar; controleer de verbinding met Open-Meteo") from None
            weather_cached = True
    # An earlier training-window horizon must not truncate the live forecast.
    predictor.weatherstore.known_source_horizon = None
    future = await predictor.prepare_dataframe(start, end)
    future_features = future.drop(columns=["price"]).dropna()
    prediction = future_features.copy()
    prediction["price"] = predictor.predictor.predict(future_features)
    rows = []
    for timestamp, row in prediction.iterrows():
        value = float(row["price"])
        stamp = timestamp.to_pydatetime()
        if start <= stamp < end and math.isfinite(value):
            # Upstream price unit is ct/kWh; adapter contract is bare EUR/MWh.
            rows.append({"startsAt": stamp.isoformat(), "total": value * 10})
    if not rows:
        raise ValueError("Geen lokale Epex-prognose beschikbaar")
    return {"prices": rows, "training_rows": len(training), "stage": "NL single-stage",
            "trained_at": issue.isoformat(), "weather_cached": weather_cached}


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("Lokale rekenopdracht ontbreekt")
        raw = sys.stdin.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Te grote lokale rekenopdracht")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or type(payload.get("threads")) is not int or not 1 <= payload["threads"] <= 4:
            raise ValueError("Ongeldig aantal CPU-threads")
        operation = sys.argv[1]
        if operation == "timestral_download":
            result = download_timestral(payload)
        elif operation == "timestral":
            result = infer_timestral(payload)
        elif operation == "epex":
            import asyncio
            result = asyncio.run(infer_epex(payload))
        else:
            raise ValueError("Onbekende lokale rekenopdracht")
        print(json.dumps(result, allow_nan=False))
    except Exception as exc:
        # Fixed public messages, no raw transport URLs, tokens, or training arrays.
        message = str(exc) if isinstance(exc, ValueError) else "Lokale ML-module mislukt (" + type(exc).__name__ + "); controleer data, geheugen en netwerk"
        print(json.dumps({"error": message}))
        sys.exit(1)


if __name__ == "__main__":
    main()
