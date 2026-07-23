# Third-party notices

This App is derived from [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition), copyright 2025 Lennard Beers, licensed under the MIT License included in `LICENSE.md`.

Speaker embeddings are produced with [Resemblyzer](https://github.com/resemble-ai/Resemblyzer), copyright 2019 Resemble AI, licensed under the Apache License 2.0. Resemblyzer includes pretrained model weights distributed by that project. See its installed package metadata and upstream repository for the full license text.

Noise suppression is provided by [DeepFilterNet2](https://github.com/Rikorose/DeepFilterNet) at commit `d375b2d8309e0935d165700c91da9de862a99c31`, copyright Hendrik Schröter and contributors, dual-licensed under MIT or Apache License 2.0. The distributed `DeepFilterNet2.zip` checkpoint is checksum-pinned in the Dockerfile.

Target-speaker extraction uses the SpEx+ architecture from [ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio) at commit `6b3774dc79c46ae8bed2a4fa5f706f0ac8c75c61`. The retained architecture is copyright 2020 Meng Ge and MIT licensed. The pretrained checkpoint from [alibabasglab/log_wsj0-2mix_speech_SpEx-plus_2spk](https://huggingface.co/alibabasglab/log_wsj0-2mix_speech_SpEx-plus_2spk) is fixed at revision `2b99a144297ac4d074bb9dcc4ce9734a7e8924fd` and checksum-pinned in the Dockerfile.

The container also distributes Python dependencies under their respective licenses, including PyTorch, DeepFilterNet, FastAPI, Uvicorn, NumPy, Pydantic, librosa and WebRTC VAD.
