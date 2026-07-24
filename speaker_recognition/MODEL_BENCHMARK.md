# DeepFilterNet2 validation for 2.1.2

Validation is performed on amd64 with four CPU cores and a hard 2 GiB
container memory limit. The image contains DeepFilterNet2 only; SpEx+ and all
target-speaker separation code and weights were removed.

## Measurement policy

The offline smoke test processes the same deterministic five-second
speech-like clip twice:

1. the cold run loads DeepFilterNet2 and reports `model_load_ms`,
   `cold_start_ms` and `cold_request_ms`;
2. the warm run reuses the loaded model and is the only run allowed to report
   comparable `denoise_ms` and `audio_processing_ms`.

This prevents model initialization from contaminating repeatable inference
figures. Cold-start latency remains visible as operational data, but is never
presented as a comparable denoise result.

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

The cold result deliberately contained no `denoise_ms` or
`audio_processing_ms`.

## Earlier Home Assistant VM observation

On the first 5.27-second Home Assistant Voice recording tested before the
denoise-only rebuild, the DeepFilterNet2 stage took 2.32 seconds while loading
the model. A subsequent warm run took 0.19 seconds. The denoise-only container
result above is authoritative for the final package.
