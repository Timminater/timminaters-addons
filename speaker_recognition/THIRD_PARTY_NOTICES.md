# Third-party notices

This App is derived from [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition), copyright 2025 Lennard Beers, licensed under the MIT License included in `LICENSE.md`.

Speaker embeddings are produced with [Resemblyzer](https://github.com/resemble-ai/Resemblyzer), copyright 2019 Resemble AI, licensed under the Apache License 2.0. Resemblyzer includes pretrained model weights distributed by that project. See its installed package metadata and upstream repository for the full license text.

Noise suppression is provided by [DeepFilterNet2](https://github.com/Rikorose/DeepFilterNet) at commit `d375b2d8309e0935d165700c91da9de862a99c31`, copyright Hendrik Schröter and contributors, dual-licensed under MIT or Apache License 2.0. The distributed `DeepFilterNet2.zip` checkpoint is checksum-pinned in the Dockerfile.

The container also distributes Python dependencies under their respective licenses, including PyTorch, DeepFilterNet, FastAPI, Uvicorn, NumPy, Pydantic, librosa and WebRTC VAD.
