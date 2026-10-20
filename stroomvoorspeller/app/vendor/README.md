# Bundled forecasting code

- `epexpredictor`: model modules from b3nn0/EpexPredictor commit
  676ed37da4968c4a94d3fece39b948d5988ff580, BSD-3-Clause (LICENSE included).
  Local adaptations: respect inherited HTTPS proxy/CA and bound network timeouts;
  do not interpolate missing market quarters; bound LightGBM to two CPU threads
  with a deterministic seed. Trailing whitespace normalized. The add-on runs the original NL single-stage model;
  this is not the public API's full multi-country two-stage stack.
- `timestral`: mistralai/timestral commit
  a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff, Apache-2.0 (LICENSE included), only trailing whitespace normalized.
  Weights are separate, NOT bundled: explicit download of Timestral-209M checkpoint
  9874d252ab0aae16f7f7c999f98f98d6ad7d8365 is required. Its Modified MIT model
  license is displayed in the download UI and downloaded with the weights.

Only the local child process imports these packages. Neither forecasting library
is imported by the web server or the default adaptive method.
