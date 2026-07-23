# Speaker Recognition

Lokale stemherkenning voor Home Assistant met een ingebouwde Ingress-interface. De App gebruikt [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) voor stem-embeddings en levert een companion-integratie met STT- en conversation-proxy's.

## Functies

- Enrollment via upload, browsermicrofoon of een bestaand Home Assistant Voice-apparaat.
- Meerdere permanente WAV-samples per profiel, inclusief afspelen, downloaden, activeren, deactiveren en verwijderen.
- Multi-window-herkenning met eenvoudige spraaksegmentdetectie, confidence, marge en kandidaat-scores.
- Een globale pipeline-policy: onbekende stemmen toestaan of blokkeren en doelstemisolatie uit, vergelijken of vóór STT toepassen.
- Lokale ruisonderdrukking met DeepFilterNet2 en enrollment-gestuurde doelstemisolatie met SpEx+.
- Zeven dagen analysehistorie van gewone Assist-pipelines en handmatige tests, met transcript, timings, diagnose en originele, ruisonderdrukte en geïsoleerde audio.
- Fragmentselectie uit een analyse-opname om een bestaand of nieuw profiel te verbeteren.
- Een kalibratiewizard die op basis van de opgeslagen samples een conservatieve drempel adviseert.
- Veilige persoonscontext voor een vervolgagent, zonder ooit Home Assistant-gebruikersrechten te wijzigen.
- Twee tijdelijke diagnostische sensoren voor de laatste herkenning en de doorgifte aan de conversation-agent.

## Installatie

1. Voeg `https://github.com/Timminater/timminaters-addons` toe onder **Instellingen → Apps → App store → Repositories**.
2. Installeer en start **Speaker Recognition**.
3. Herstart Home Assistant Core na de eerste installatie, zodat de meegeleverde custom integration wordt geladen.
4. Bevestig de gevonden Speaker Recognition App onder **Instellingen → Apparaten & diensten → Ontdekt**.
5. Voeg daarna via dezelfde integratie een STT-proxy rond je normale STT-engine en, indien gewenst, een conversation-proxy rond je normale gespreksagent toe.
6. Selecteer beide proxy-entiteiten in de Assist-pipeline van je Voice-apparaat.

Open vervolgens de App-webinterface en leg per persoon liefst 2–3 heldere fragmenten van 5–30 seconden vast. Gebruik voor een eerlijke controle andere audio dan de enrollment-samples.

Alleen `amd64` wordt momenteel gepubliceerd. De modellen zitten offline in de image en downloaden tijdens gebruik niets. Geef de Home Assistant-VM voor 2.1.0 bij voorkeur 6 GB RAM; de modelworker wordt na vijf minuten inactiviteit ontladen en heeft in de containersmoketest minder dan 500 MiB gebruikt.

Zie [DOCS.md](DOCS.md) voor de werking, instellingen, opslag en privacy-informatie.

## Herkomst

Deze implementatie is gebaseerd op het MIT-gelicentieerde project [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition). De onderzochte forks en verwerking staan in [FORK_AUDIT.md](FORK_AUDIT.md).
