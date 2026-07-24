# Third-party notices

This App is derived from [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition), copyright 2025 Lennard Beers, licensed under the MIT License included in `LICENSE.md`.

Speaker embeddings are produced with [Resemblyzer](https://github.com/resemble-ai/Resemblyzer), copyright 2019 Resemble AI, licensed under the Apache License 2.0. Resemblyzer includes pretrained model weights distributed by that project. See its installed package metadata and upstream repository for the full license text.

Noise suppression is provided by [DeepFilterNet2](https://github.com/Rikorose/DeepFilterNet) at commit `d375b2d8309e0935d165700c91da9de862a99c31`, copyright Hendrik Schröter and contributors, dual-licensed under MIT or Apache License 2.0. The distributed `DeepFilterNet2.zip` checkpoint is checksum-pinned in the Dockerfile.

Optional stateful noise suppression is derived from [vahidkowsari/pipecat-deepfilternet-stream](https://github.com/vahidkowsari/pipecat-deepfilternet-stream) at commit `212c7f684d41159b897a986ddfbb7ad667405ccd`, copyright Vahid Kowsari and contributors, licensed under the Apache License 2.0. Its source archive and bundled DeepFilterNet3 ONNX models are checksum-pinned in the Dockerfile. Local changes add an explicit SOXR/model-lookahead drain and duration accounting.

[Pipecat](https://github.com/pipecat-ai/pipecat) is pinned to version 1.6.0 and licensed under the BSD 2-Clause License. The stateful route also uses DeepFilterLib 0.5.6 (MIT OR Apache-2.0), tract 0.21.17 (MIT OR Apache-2.0), SOXR 1.0.0 (LGPL-2.1-or-later for libsoxr; Python binding metadata applies), and Loguru 0.7.3 (MIT).

The container also distributes Python dependencies under their respective licenses, including PyTorch, DeepFilterNet, FastAPI, Uvicorn, NumPy, Pydantic, librosa and WebRTC VAD.
