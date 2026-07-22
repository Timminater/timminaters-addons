# Speaker Recognition

Lokale speaker-herkenning voor Home Assistant met een ingebouwde Ingress-interface. De App gebruikt [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) om stem-embeddings te maken; ruwe audio wordt niet opgeslagen.

De App levert ook de bijpassende custom integration mee. Bij elke start wordt deze onder
`/homeassistant/custom_components/speaker_recognition` bijgewerkt en via Supervisor-discovery
aangemeld. Herstart Home Assistant Core na de eerste installatie eenmaal; daarna verschijnt
de App onder **Instellingen > Apparaten & diensten > Ontdekt**.

## Functies

- Enroll speakers via audioupload, browsermicrofoon of een bestaand Home Assistant Voice-apparaat.
- Koppel een stemprofiel optioneel aan een Home Assistant `person.*`-entiteit.
- Combineer meerdere samples tot één genormaliseerd stemprofiel.
- Herken een speaker met een apart testfragment en een instelbare confidence-drempel.
- Maak een STT-proxy én een selecteerbare conversation-proxy rond bestaande Home Assistant-entiteiten.
- Gebruik persoonsherkenning veilig als personalisatiecontext, nooit als authenticatie of rechtenbron.
- Beheer en verwijder profielen via Home Assistant Ingress.
- Bewaar profielen atomisch onder `/data`, inclusief restart en App-backups.
- Optionele token-beveiligde REST-API; de hostpoort staat standaard uit.

## Installatie

1. Voeg `https://github.com/Timminater/timminaters-addons` toe onder **Instellingen → Apps → App store → Repositories**.
2. Installeer **Speaker Recognition**.
3. Start de App en kies **Open webinterface**.
4. Voeg per speaker liefst 2–3 heldere fragmenten van 5–30 seconden toe.

Voor enrollment en herkenning via een Voice-apparaat voeg je in de Speaker Recognition-
integratie een STT-proxy toe rond je normale STT-engine. Gebruik daarna een Assist-pipeline
met die proxy als STT-engine voor het Voice-apparaat. De GUI kan het apparaat vervolgens
zelf laten luisteren.

Voeg de integratie nogmaals toe en kies **Conversation-proxy toevoegen** om een bestaande
conversation-agent te koppelen. De ontstane `conversation.*`-entiteit kan vervolgens als
agent in een Assist-pipeline worden gekozen. De backend-URL en companion-token staan onder
**Configureren** bij de hoofdentry en worden vóór opslaan gecontroleerd.

Op dit moment wordt alleen `amd64` aangeboden. PyTorch/Resemblyzer is groot en de upstream ARM64-builds zijn niet betrouwbaar genoeg om als ondersteund te publiceren.

Zie [DOCS.md](DOCS.md) voor instellingen, API en privacy-informatie.

## Herkomst

Deze implementatie is gebaseerd op het MIT-gelicentieerde project [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition). De onderzochte forkverbeteringen en hun verwerking staan in [FORK_AUDIT.md](FORK_AUDIT.md).
