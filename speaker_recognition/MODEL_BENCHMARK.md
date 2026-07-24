# DeepFilterNet2 validation for 2.1.2

Validation is performed on amd64 with four CPU cores and a hard 2 GiB
container memory limit. The image contains DeepFilterNet2 only; SpEx+ and all
target-speaker separation code and weights were removed.

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
