# DeepFilterNet2 validation for 2.1.2

## Speaker identity and end-to-end quality matrix

`tests/live_quality_matrix.py` is a manual integration benchmark against a
running App. It submits each case through `/api/analyze`, waits for completed
processing, and records whole-request wall time from submission through final
status. Use `--repeats` (default 3) to report per-case p50/p95/p99 and mean.
Those percentiles describe only the supplied fixture set and repeat count; they
are not population-level guarantees. Repeat counts below 100 provide a coarse
p99 estimate, so retain individual runs and report the sample count.

The clean target fixture must match `--speaker-name`. Declare the competitor
fixture with exactly one of `--competitor-speaker NAME` or
`--competitor-unknown`; an unknown fixture must remain `unmatched` or
`ambiguous` and must not name a speaker. Wrong identity is a hard failure.
The other synthetic stress cases are diagnostic, not a substitute for
independently collected and labelled unknown speakers, different days/devices,
distances, short and long Dutch commands, or background conditions. Synthetic
noise/music mixtures do not establish real-world quality.

Example invocation (use an isolated test App and disposable profiles):

```text
python tests/live_quality_matrix.py --speaker-name Alice --target fixtures/alice.wav --competitor fixtures/unknown.wav --competitor-unknown --repeats 20
```

This script stores analysis recordings in the App's normal analysis history.
Use non-sensitive test audio or an isolated App data directory, and remove the
generated recordings and temporary test profiles afterward. The report
contains speaker names and diagnostic measurements: review it before sharing
and replace names with pseudonyms when needed. Never include tokens in a
benchmark report.

Model comparisons (Resemblyzer baseline, CAM++, ReDimNet2 B1/B2, ERes2NetV2,
and ECAPA-TDNN reference) must reuse the same labelled train/calibration/test
partition and production decision path. Keep speakers and recording sessions
separated between calibration and test partitions. Report false accepts of
unknown voices and wrong identities separately from missed known speakers;
wrong identity is the more costly error. Report p50/p95/p99 end-to-end latency,
CPU, memory, and concurrency conditions alongside quality. Do not claim a
model win from historical figures, a self-comparison, or synthetic audio.
Promote a fast/accurate selector only when both choices show a reproducible,
measurable benefit on that independent set. A model switch requires recomputing
embeddings from retained WAVs and recalibration; profiles without WAVs need
re-enrollment.

GPU/OpenVINO work is an experiment, not a supported runtime claim. First run
the same fixtures on CPU, OpenVINO CPU, and OpenVINO GPU outside the production
VM; then measure encoder inference, complete recordings, and only then DF3
10 ms blocks. Record actual device selection, fallback, quality, end-to-end
latency, CPU, and memory. Device passthrough alone is not evidence of
acceleration. No GPU or hardware result is asserted by this document.

## Lokale gelabelde testset

Voor een echte identity-evaluatie gebruik je
`tests/quality_dataset_benchmark.py` met een JSON- of CSV-manifest. Het
programma leest mono PCM16 WAV's lokaal en stuurt ze naar de vluchtige
`POST /api/recognize`-route. Die route geeft de herkenning terug zonder een
analyse-opname te maken. De audio blijft op de machine waarop het script draait
en wordt alleen in de API-aanvraag verstuurd. Gebruik een lokaal/test-App en
zet `SPEAKER_RECOGNITION_TOKEN` als omgevingsvariabele; het token wordt niet als
CLI-argument of in het rapport opgenomen. Een optioneel `--output`-rapport
bevat case-id's en labels, dus houd het lokaal of pseudonimiseer die waarden.

JSON-voorbeeld met uitsluitend fictieve labels en waarden:

```json
{
  "cases": [
    {"id": "sample-a-01", "path": "wav/sample-a-01.wav", "expected": "speaker_a", "day": "day_1", "device": "mic_a", "condition": "quiet"},
    {"id": "sample-b-01", "path": "wav/sample-b-01.wav", "expected": "speaker_b", "day": "day_2", "device": "mic_b", "condition": "background_noise"},
    {"id": "unknown-01", "path": "wav/unknown-01.wav", "expected": "unknown", "day": "day_3", "device": "mic_a", "condition": "quiet"}
  ]
}
```

CSV gebruikt dezelfde kolommen: `id,path,expected,day,device,condition`.
Audio-paden relatief aan het manifest worden vanaf de manifestmap opgelost.
Voor bekende stemmen moet `expected` overeenkomen met de in de test-App
ingeschreven pseudoniem; gebruik `unknown` voor een stem die niet in de
profielen staat. Elk onbekend voorbeeld dat wordt geaccepteerd en elke
bekende stem die aan de verkeerde persoon wordt gekoppeld is een harde fout
(exitcode 1). Een gemiste bekende stem wordt apart geteld, zodat herkenning
die veilig afwijst niet wordt verward met een persoonsverwisseling.

Het rapport geeft aantallen per uitkomst en latency p50/p95/p99/mean voor de
hele set en afzonderlijk per verwachte klasse, dag, apparaat en conditie.
Percentielen zijn beschrijvend voor de opgegeven fixtures; p99 uit een kleine
set is instabiel. Neem meerdere onafhankelijke dagen en voldoende voorbeelden
per relevante groep op en bewaar het aantal `n` bij iedere latencygroep. Splits
kalibratie- en testdata op spreker én opnamedag voordat je thresholds instelt;
gebruik testdata niet om drempels achteraf te kiezen. Dit harnas past geen
drempels aan en downloadt of traint geen modellen.

Validation is performed on amd64 with four CPU cores and a hard 2 GiB
container memory limit. The default route uses DeepFilterNet2 only; SpEx+ and
all target-speaker separation code and weights were removed. The optional,
non-default DF3 route is tracked separately in
[DF3_STREAMING_VALIDATION.md](DF3_STREAMING_VALIDATION.md).

## Measurement policy

The offline smoke test processes the same deterministic five-second
speech-like clip twice after explicitly preloading DeepFilterNet2:

1. startup loads DeepFilterNet2 before any user audio is submitted;
2. the first user run must already report comparable `denoise_ms` and
   `audio_processing_ms`;
3. a repeated run verifies that the same resident worker remains warm.

This prevents model initialization from contaminating repeatable inference
figures. Startup preload time is reported separately from both user-triggered
runs.

Every accepted output must be mono PCM, retain the original duration within
50 ms, remain below one percent clipping and complete with the whole container
limited to 2 GiB. Version 2.1.2 preloads the worker at add-on startup and keeps
it resident until shutdown, so user-triggered runs use the comparable warm path.

## Denoise-only container result

The final local release-image run produced:

- cold request: 3.093 seconds;
- model initialization reported separately: 0.132 seconds;
- warm comparable `denoise_ms`: 0.318 seconds;
- warm `audio_processing_ms`: 0.322 seconds;
- peak child-process memory: 323.1 MiB.

These historical figures were recorded before startup preloading was enabled.
The 2.1.2 smoke test now reports preload time separately and requires both
subsequent denoise runs to use the comparable warm path.

## Earlier Home Assistant VM observation

On the first 5.27-second Home Assistant Voice recording tested before the
denoise-only rebuild, the DeepFilterNet2 stage took 2.32 seconds while loading
the model. A subsequent warm run took 0.19 seconds. The denoise-only container
result above is authoritative for the final package.
