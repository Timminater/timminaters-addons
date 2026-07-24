# Speaker Recognition

Lokale stemherkenning voor Home Assistant met een ingebouwde Ingress-interface. De App gebruikt [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) voor stem-embeddings en levert een companion-integratie met STT- en conversation-proxy's.

## Functies

- Enrollment via upload, browsermicrofoon of een bestaand Home Assistant Voice-apparaat.
- Meerdere permanente WAV-samples per profiel, inclusief afspelen, downloaden, activeren, deactiveren en verwijderen.
- Multi-window-herkenning met spraaksegmentdetectie, confidence, marge en kandidaat-scores.
- Een globale pipeline-policy: onbekende stemmen toestaan of blokkeren en ruisonderdrukking uit, vergelijken of vóór STT toepassen.
- Optionele lokale ruisonderdrukking met DeepFilterNet2; standaard blijft deze uitgeschakeld.
- Zeven dagen analysehistorie met transcript, timings, diagnose en originele en ruisonderdrukte audio.
- Fragmentselectie uit een analyse-opname om een bestaand of nieuw profiel te verbeteren.
- Een kalibratiewizard die op basis van de opgeslagen samples een conservatieve drempel adviseert.
- Veilige persoonscontext voor een vervolgagent, zonder Home Assistant-gebruikersrechten te wijzigen.
- Twee tijdelijke diagnostische sensoren voor de laatste herkenning en de doorgifte aan de conversation-agent.

## Installatie

1. Voeg `https://github.com/Timminater/timminaters-addons` toe onder **Instellingen → Apps → App store → Repositories**.
2. Installeer en start **Speaker Recognition**.
3. Herstart Home Assistant Core na de eerste installatie, zodat de meegeleverde custom integration wordt geladen.
4. Bevestig de gevonden Speaker Recognition App onder **Instellingen → Apparaten & diensten → Ontdekt**.
5. Voeg via dezelfde integratie een STT-proxy rond je normale STT-engine en eventueel een conversation-proxy rond je gespreksagent toe.
6. Selecteer beide proxy-entiteiten in de Assist-pipeline van je Voice-apparaat.

Leg per persoon liefst 2–3 heldere fragmenten van 5–30 seconden vast. Gebruik voor een eerlijke controle andere audio dan de enrollment-samples.

Alleen `amd64` wordt gepubliceerd. De modellen zitten offline in de image en downloaden tijdens gebruik niets. Geef de Home Assistant-VM bij voorkeur 6 GB RAM. De DeepFilterNet2-worker wordt na vijf minuten inactiviteit ontladen; koude modelstarts worden apart gemeten en tellen niet mee als vergelijkbare denoise-tijd.

Zie [DOCS.md](DOCS.md) voor de werking, instellingen, opslag en privacy-informatie.

## Herkomst

Deze implementatie is gebaseerd op het MIT-gelicentieerde project [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition). De onderzochte forks en verwerking staan in [FORK_AUDIT.md](FORK_AUDIT.md).
