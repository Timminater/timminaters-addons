# Speaker Recognition

Lokale stemherkenning voor Home Assistant met een ingebouwde Ingress-interface. De App gebruikt [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) voor stem-embeddings en levert een companion-integratie met STT- en conversation-proxy's.

## Functies

- Enrollment via upload, browsermicrofoon of een bestaand Home Assistant Voice-apparaat.
- Meerdere permanente WAV-samples per profiel, inclusief afspelen, downloaden, activeren, deactiveren en verwijderen.
- Multi-window-herkenning met spraaksegmentdetectie, confidence, marge, kandidaat-scores en een expliciete uitkomst voor meerdere bekende sprekers.
- Een globale pipeline-policy: onbekende stemmen toestaan of blokkeren en ruisonderdrukking uit, vergelijken of vóór STT toepassen.
- Optionele lokale ruisonderdrukking met DeepFilterNet2; standaard blijft deze uitgeschakeld.
- Zeven dagen analysehistorie met transcript, timings, diagnose en originele en ruisonderdrukte audio.
- Beoordelingsinbox voor onbekende, ambigue en gemarkeerde opnamen, met handmatige sprekerlabels zonder automatisch leren.
- Opnamekiezer bij het maken van een profiel: zoek bewaarde audio, beluister en selecteer een fragment, controleer de kwaliteit en bevestig enrollment.
- Gastprofielen met een einddatum (standaard dertig dagen, aanpasbaar of nooit) en automatisch opschonen van gast-enrollment en historische namen.
- Profielen samenvoegen met herberekening van actieve opnamen en rollback bij opslagproblemen.
- Per Voice-apparaat herkennings-, onbekende-stem-, kwaliteits- en wachttijdstatistieken.
- Proefstand voor alternatieve drempels en marges op handmatig gelabelde opnamen, zonder productiewijzigingen.
- Vrijwillig te starten experimentele offline sprekerstijdlijn voor bewaarde opnamen; geen invloed op live Assist of transcript.
- Fragmentselectie uit een analyse-opname om een bestaand of nieuw profiel te verbeteren.
- Een kalibratiewizard die op basis van de opgeslagen samples een conservatieve drempel adviseert.
- Herkenningsresultaten en optionele conversation-proxy; huidige Home Assistant-versies delen geen expliciete pipeline-run-ID tussen STT en gesprek, dus persoonscontext wordt fail-closed niet doorgestuurd.
- Twee tijdelijke diagnostische sensoren met speakerlijsten en de status van contextdoorgifte.

## Installatie

1. Voeg `https://github.com/Timminater/timminaters-addons` toe onder **Instellingen → Apps → App store → Repositories**.
2. Installeer en start **Speaker Recognition**.
3. Herstart Home Assistant Core na de eerste installatie, zodat de meegeleverde custom integration wordt geladen.
4. Bevestig de gevonden Speaker Recognition App onder **Instellingen → Apparaten & diensten → Ontdekt**.
5. Voeg via dezelfde integratie een STT-proxy rond je normale STT-engine toe. Je kunt ook een conversation-proxy rond je bestaande gespreksagent instellen; die geeft het gesprek door, maar voegt op huidige Home Assistant-versies geen persoonscontext toe.
6. Selecteer de STT-proxy en eventueel de conversation-proxy in de Assist-pipeline van je Voice-apparaat.

De STT-herkenning en pipeline-policy werken zonder persoonscontext. Stemgebaseerde personalisatie wordt pas actief wanneer Home Assistant dezelfde expliciete pipeline-run-ID beschikbaar maakt bij zowel STT als conversation; de integratie weigert anders bewust om een persoon aan een gesprek te koppelen.

Leg per persoon liefst 2–3 heldere fragmenten van 5–30 seconden vast. Gebruik voor een eerlijke controle andere audio dan de enrollment-samples.

Alleen `amd64` wordt gepubliceerd. De modellen zitten offline in de image en downloaden tijdens gebruik niets. Geef de Home Assistant-VM bij voorkeur 6 GB RAM. DeepFilterNet2 wordt tijdens het starten van de add-on opgewarmd en blijft resident (in de validatietest circa 323 MiB), ook wanneer audiobewerking uitstaat. Er wordt dan geen audio verwerkt, maar een latere aanvraag heeft geen koude modelstart.

De experimentele Pipecat/DeepFilterNet3-route is expliciet te kiezen op de pagina **Instellingen**; `DF2 batch` blijft de standaard en veilige rollback. DF3 verwerkt inkomende `before_stt`-audio per 10 ms-hop en voert aan het utterance-einde beide SOXR-resamplers en model-lookahead af. In **Analyse** kan dezelfde stateful route per bestaande WAV worden gekozen. Zie [DF3_STREAMING_VALIDATION.md](DF3_STREAMING_VALIDATION.md) voor pins, draincontract, bewijs en nog openstaande acceptatietests.

Zie [DOCS.md](DOCS.md) voor de werking, instellingen, opslag en privacy-informatie.

De opnamekiezer en beoordelingsinbox kunnen alleen audio afspelen of hergebruiken zolang de analyse-WAV volgens de ingestelde retentie bestaat. Een gastprofiel zonder einddatum verloopt nooit; bij verlopen worden alleen de enrollment-opnamen en gastidentiteit uit analysedata opgeruimd, terwijl analyse-audio het normale bewaarbeleid volgt. De sprekerstijdlijn is experimenteel en gebruikt geen transcript-woordtiming of live persoonscontext.

## Herkomst

Deze implementatie is gebaseerd op het MIT-gelicentieerde project [EuleMitKeule/speaker-recognition](https://github.com/EuleMitKeule/speaker-recognition). De onderzochte forks en verwerking staan in [FORK_AUDIT.md](FORK_AUDIT.md).
