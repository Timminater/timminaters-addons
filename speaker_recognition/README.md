# Speaker Recognition

Lokale speaker-herkenning voor Home Assistant met een ingebouwde Ingress-interface. De App gebruikt [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) om stem-embeddings te maken; ruwe audio wordt niet opgeslagen.

## Functies

- Enroll speakers via audioupload of browsermicrofoon.
- Combineer meerdere samples tot één genormaliseerd stemprofiel.
- Herken een speaker met een apart testfragment en een instelbare confidence-drempel.
- Beheer en verwijder profielen via Home Assistant Ingress.
- Bewaar profielen atomisch onder `/data`, inclusief restart en App-backups.
- Optionele token-beveiligde REST-API; de hostpoort staat standaard uit.

## Installatie

1. Voeg `https://github.com/Timminater/timminaters-addons` toe onder **Instellingen → Apps → App store → Repositories**.
2. Installeer **Speaker Recognition**.
3. Start de App en kies **Open webinterface**.
4. Voeg per speaker liefst 2–3 heldere fragmenten van 5–30 seconden toe.

Op dit moment wordt alleen `amd64` aangeboden. PyTorch/Resemblyzer is groot en de upstream ARM64-builds zijn niet betrouwbaar genoeg om als ondersteund te publiceren.

Zie [DOCS.md](DOCS.md) voor instellingen, API en privacy-informatie.

## Herkomst

Deze implementatie is gebaseerd op het MIT-gelicentieerde project [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition). De onderzochte forkverbeteringen en hun verwerking staan in [FORK_AUDIT.md](FORK_AUDIT.md).
