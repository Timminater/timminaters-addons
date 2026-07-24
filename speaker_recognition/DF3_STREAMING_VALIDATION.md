# Stateful Pipecat DeepFilterNet3 validation

## Safety and activation

The production default remains `df2_batch`. The experimental route is selected
explicitly on the App's **Instellingen** page. It is used for live audio in the
`before_stt` pipeline mode, for background `compare` processing, and when a
user selects DF3 for an existing WAV in **Analyse**. Any DF3 preload, streaming,
timeout, drain, or quality failure falls back to the resident
DeepFilterNet2/PyTorch worker.

Do not make DF3 the default until every target-environment and audio-quality
criterion below has passed.

## Pinned implementation

- `vahidkowsari/pipecat-deepfilternet-stream`
  `212c7f684d41159b897a986ddfbb7ad667405ccd` (Apache-2.0).
- Source/model archive SHA-256:
  `2064706cf5488e723a3404de2f0c6a559dffbf35daf9ede0b382e2ab32ef5a60`.
- Python runtime image: pinned Python 3.11.15 slim Bookworm image.
- `pipecat-ai==1.6.0`, `DeepFilterLib==0.5.6`,
  `tract==0.21.17`, `soxr==1.0.0`, and `loguru==0.7.3`.
- `tract` must not be upgraded independently. Research showed that 0.23.3
  fails all four upstream tests because `model_for_path` is absent.
- The DF3 archive contains its ONNX models and is installed during the image
  build. Runtime inference performs no network download.
- OMP, OpenBLAS, MKL, NumExpr, and Rayon thread counts are limited to one.

The research artifacts named in the implementation task were not available in
this checkout: the equivalent
`C:\Users\timmi\Documents\Codex\2026-07-24\deepfilternet-benchmarks`
directory contained empty `outputs` and `work` directories. The independently
reported baseline is therefore recorded, but its environment lock could not
be compared byte-for-byte.

## Drain contract

Pipecat 1.6.0's SOXR wrapper never calls
`ResampleStream.resample_chunk(..., last=True)`, and the upstream filter's
`stop()` drops its engine, resamplers, and partial 48 kHz buffer. This App owns
utterance finalization instead:

1. flush the 16 -> 48 kHz SOXR stream with `last=True`;
2. zero-pad only the final incomplete 480-sample hop;
3. process three zero hops to expose the measured 30 ms delayed model tail;
4. omit the corresponding first three delayed output hops;
5. flush the 48 -> 16 kHz SOXR stream with `last=True`;
6. trim only surplus resampler/padding samples to the exact source count;
7. reject the DF3 result and use DF2 if the drained output is shorter.

`post_utterance_ms` starts immediately before step 1 and includes both SOXR
flushes, the padded hop, model-lookahead drain, and final duration accounting.
`audio_processing_ms` uses this user-visible value. Compute time spread during
the utterance is reported separately as `stream_compute_ms`; it is not added
to pipeline latency as though processing were batch.

## Evidence currently available

- Local unit/integration/UI-contract suite: 91 passed.
- Explicit drain coverage: hop-aligned and non-hop-aligned lengths, silence,
  very short PCM, 5/10/30 seconds, processing before EOF, single finalization,
  and fresh state between utterances.
- Companion coverage: split WAV headers, stereo downmix, chunked request path,
  processed audio selection, and DF2-compatible fallback contracts.
- The supplied research baseline for the full 16 -> 48 -> DF3 -> 16 kHz path
  remains: 5 seconds, compute p50 1006.50 ms, p95 1057.90 ms, RTF 0.2013,
  peak RSS 126.74 MiB, per-call p99 4.15 ms, and 3 deadline misses per 10,000
  blocks. Correlation against official v0.5.6 tract DF3 after alignment was
  0.999999983 with 73.55 dB SNR.

## Still required before default activation

- Build the image and run the real tract/DeepFilterLib/SOXR tests on Linux.
  Docker was installed locally but its daemon was not running, so no image was
  built in this work session.
- After building `speaker-recognition:df3-local`, run
  `docker run --rm -v "${PWD}/tests:/app/tests:ro" --entrypoint python
  speaker-recognition:df3-local tests/df3_stream_benchmark.py --repeats 5`
  from this directory to produce the 5/10/30-second latency/jitter/CPU/RSS
  report.
- On the Home Assistant VM, record warm post-utterance p50/p95/p99, compute
  jitter/deadline misses, CPU, and peak RSS for 5/10/30-second fixtures.
- Confirm warm post-utterance p95 is at most 100 ms, or measure an end-to-end
  user-latency improvement over the current approximately 322 ms warm batch.
- Run Dutch STT WER and speaker-confidence/margin A/B tests on real fixtures,
  including final-word checks and noisy/silent/short utterances.
- Confirm output duration differs by no more than 50 ms (the implementation
  expects exactly 0 ms) and inspect that no final word is truncated.
- Exercise DF3 failure injection and DF2 rollback in the built container.
- Soak-test within the 6 GB Home Assistant VM budget.

Required user action: start a local Docker daemon and request a local build, or
explicitly authorize a build/test on the Home Assistant VM. No configuration,
restart, installation, publication, or push has been performed.
