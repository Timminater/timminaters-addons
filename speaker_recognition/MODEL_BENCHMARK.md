# Model validation for 2.1.0

Validation was performed on amd64 with four CPU cores and a hard 2 GiB
container memory limit.

## Bundled pipeline

The offline container smoke test processes five seconds of deterministic
speech-like audio without network access. DeepFilterNet2 and SpEx+ both
returned mono PCM with the original duration:

- total cold processing time: 3.72 seconds;
- peak model-process memory: 472.7 MiB;
- warm target-isolation stage on the live fixture: approximately 0.50 seconds.

The live quality matrix used an enrollment recording for the target speaker
and a second public voice as interference. It covered clean audio, constant
noise, music-like tones, two simultaneous speakers, absent target, silence and
clipping. The absent target and silence were rejected. Every accepted output
preserved duration and remained below one percent clipping.

For the two-speaker fixture, the SpEx+ output correlation was 0.3246 with the
target and 0.0073 with the competitor.

## SepFormer-WHAMR research comparison

SpeechBrain 1.0.3 with `speechbrain/sepformer-whamr` was tested separately
under the same 2 GiB and four-CPU limit. It is not shipped in the App.

- model load: 13.65 seconds;
- cold inference: 3.95 seconds;
- warm inference: 3.75 seconds;
- peak process memory: 826.5 MiB;
- selected output correlation: 0.5661 target, 0.0448 competitor.

SepFormer fit the time and memory ceilings, but passed over six times as much
competitor correlation as SpEx+ on the same mixture. It therefore failed the
“less competing speech” replacement condition. A WER result could not reverse
that conjunctive decision and was not used to replace SpEx+. The final
Home Assistant Voice A/B remains the release check for transcript quality.
