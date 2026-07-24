# Speaker Recognition-documentatie

## Companion-integratie

De App installeert of actualiseert bij het starten de meegeleverde `speaker_recognition` custom integration onder `/homeassistant/custom_components` en meldt de backend aan via Supervisor-discovery. Herstart Home Assistant Core na de eerste installatie. Daarna verschijnt de App onder **Instellingen > Apparaten & diensten > Ontdekt**.

Een al aanwezige integratiemap die niet door deze App wordt beheerd, wordt eerst bewaard als `speaker_recognition.pre-app-backup`. Het verwijderen van de App herstelt die map niet automatisch.

Voeg de integratie daarna nogmaals toe voor:

1. een **STT-proxy** rond de STT-engine van de Assist-pipeline; en
2. optioneel een **conversation-proxy** rond de bestaande conversation-agent.

De STT-proxy laat dezelfde audiostream herkennen en stuurt hem volgens de globale policy door naar STT. De conversation-proxy kan een verse, exact aan dezelfde Voice-satelliet gekoppelde stemmatch of lijst met meerdere bekende sprekers als niet-vertrouwde personalisatiecontext aanbieden. Bij meerdere sprekers wordt expliciet gemeld dat losse woorden of opdrachten niet automatisch aan één persoon mogen worden toegeschreven. Tekst, taal, conversation-id, device/satellite-id en vooral het oorspronkelijke Home Assistant `Context` blijven behouden. Een stemmatch verandert nooit `Context.user_id`, authenticatie of rechten.

## Profielen en enrollment

Op de pagina **Profielen** kun je samples uploaden of opnemen met de browser of een Home Assistant Voice-apparaat. Gebruik bij voorkeur:

- 2–3 samples per persoon;
- 5–30 seconden duidelijke, natuurlijke spraak;
- verschillende voorbeeldzinnen en opnamemomenten;
- zo min mogelijk muziek, galm en andere stemmen.

De voorbeeldtekst is alleen een hulpmiddel. De App kiest willekeurig uit meerdere makkelijk leesbare zinnen; letterlijk voorlezen is niet verplicht.

Elk enrollmentfragment wordt als WAV permanent onder `/data/enrollment` opgeslagen. Per profiel kun je samples afspelen, downloaden, activeren, deactiveren of definitief verwijderen. Bij het vervangen van een profiel worden oude samples inactief, niet verwijderd. Bij het verwijderen van een profiel vraagt de GUI altijd of de bijbehorende audio moet worden verwijderd of gearchiveerd.

Een profiel kan aan een `person.*`-entiteit worden gekoppeld. Dit is uitsluitend metadata voor diagnose en ongevaarlijke personalisatie.

### Home Assistant Voice gebruiken

Het Voice-apparaat moet een Assist-pipeline gebruiken waarvan de STT-engine de Speaker Recognition STT-proxy is. De GUI start een eenmalige `assist_satellite.ask_question`-opname en onderschept alleen de STT-stream. Enrollmentspraak bereikt de conversation- of intentlaag niet. Na de opname kun je het fragment eerst terugluisteren.

## Herkenning en pipeline-policy

De herkenner beoordeelt de volledige uiting, spraakregio's en overlappende tijdvensters. De hoogste overeenkomst bepaalt de kandidaat. Een resultaat is alleen een match als zowel de confidence-drempel als de minimale marge ten opzichte van de tweede kandidaat wordt gehaald. Die scoremarge is `beste score - tweede score`: bij `0` is deze extra ambiguïteitscontrole uitgeschakeld; een hogere waarde vermindert persoonsverwisselingen maar kan vaker een ambigu of onbekend resultaat geven. Een toegepaste kalibratie gebruikt haar berekende marge in plaats van de basiswaarde uit **Instellingen**.

De globale policy in de webinterface bevat:

- **Onbekende speaker toestaan** (standaard): STT en conversation blijven werken wanneer niemand wordt herkend.
- **Onbekende speaker blokkeren**: een onbekende of ambigue uiting stopt vóór de conversation-agent. Gebruik dit niet als beveiligingsmiddel voor sloten, alarmen of andere gevoelige acties.
- **Audiobewerking uit** (standaard): STT ontvangt de oorspronkelijke audio.
- **Alleen vergelijken**: de App maakt DeepFilterNet2-ruisonderdrukking op de achtergrond; STT ontvangt nog steeds het origineel.
- **Vóór STT** (experimenteel): de App probeert binnen maximaal twaalf seconden de ruisonderdrukte audio aan STT te geven. Bij tijdsoverschrijding, kwaliteitsafkeur of een modelfout ontvangt STT het origineel.

DeepFilterNet2 werkt intern op 48 kHz en levert 16 kHz mono-PCM met dezelfde tijdlijn terug. Clips tot maximaal 120 seconden worden ondersteund. Live STT heeft voorrang op handmatige Analyse-taken. De modelworker wordt tijdens het starten van de add-on opgewarmd en blijft resident tot de add-on stopt. Daardoor zijn gebruikersaanvragen vanaf de eerste opname warm en onderling vergelijkbaar. Het resident houden activeert de audiobewerking niet: in modus `off` blijft de worker alleen gereed in het geheugen.

De backendkeuze op de pagina **Instellingen** staat standaard op `DF2 batch`
en wordt lokaal bewaard; toegang tot `config.yaml` is niet nodig.
`DF3 stateful streaming` activeert voor **Vóór STT** de experimentele stateful
Pipecat/DeepFilterNet3-route. De companion streamt WAV/PCM dan tijdens de
opname naar de App; de 16 → 48 → 16 kHz-keten wordt dus niet na afloop als
batch gestart. Aan het einde worden input-SOXR, een eventuele gedeeltelijke
hop, drie model-lookaheadhops en output-SOXR expliciet afgevoerd. Bij iedere
fout of afgekeurde kwaliteit gebruikt dezelfde aanvraag de resident DF2-route.
Laat `DF2 batch` geselecteerd totdat de doelomgeving- en kwaliteitseisen in
[DF3_STREAMING_VALIDATION.md](DF3_STREAMING_VALIDATION.md) zijn bewezen.

Wanneer de backend niet bereikbaar is, blijft de normale `allow`-policy fail-open. Een actief bekende `block`-policy faalt gesloten.

## Analyse

De pagina **Analyse** bewaart gewone Assist-pipeline-opnamen en opnamen van **Test een fragment**. De generieke externe `/api/recognize`-route blijft vluchtig en wordt niet gelogd.

Per item zijn, voor zover beschikbaar, zichtbaar:

- aparte spelers voor origineel en ruisonderdrukt;
- transcript en bron/satelliet;
- match, confidence, drempel, marge en alle profiel-scores;
- gebruikte segmenten en het beste tijdvenster;
- herkennings-, warme denoise-, model-laad-, STT- en totale verwerkingstijd;
- modelstappen, kwaliteitsmetingen, gebruikte audiovariant, fallbackreden, blokkering en doorgifte aan de conversation-agent.

De herkenner vergelijkt daarnaast overtuigende winnaars in niet-overlappende
spraakregio's. Wanneer verschillende bekende profielen afzonderlijke regio's
winnen, wordt de uitkomst **Meerdere sprekers**. Deze uitkomst blijft onder de
blokkeerpolicy toegestaan, omdat alle gemelde stemmen bekende profielen zijn.
Gelijktijdig door elkaar praten kan zonder een zwaarder diarization- of
stemseparatiemodel niet betrouwbaar aan afzonderlijke personen worden gekoppeld.

Met **Opnieuw analyseren** wordt de originele WAV opnieuw beoordeeld met de
actuele stemprofielen, herkenningsdrempel, scoremarge en toegepaste kalibratie.
Alleen het herkenningsresultaat wordt vervangen; transcript, audiovarianten,
STT-metingen en historische conversation-context blijven behouden. De actuele
`person.*`-koppeling van de herkende speaker wordt daarom apart getoond van de
persoon die eventueel tijdens het oorspronkelijke gesprek is gebruikt.

Met **Ruis onderdrukken** start je een asynchrone verwerking zonder een profiel
te kiezen. Per uitvoering kun je DF2 batch of DF3 stateful streaming selecteren;
DF3 leest de bestaande WAV in begrensde blokken en gebruikt dezelfde drainroute
als live audio. Met **Ruisonderdrukking wissen** verwijder je alleen de afgeleide
WAV en verwerkingsmetingen, zodat je opnieuw kunt verwerken. Origineel,
transcript en herkenningsresultaat blijven behouden. De golfvormselectie blijft
uitsluitend bedoeld om een handmatig gekozen deel aan een bestaand of nieuw
enrollmentprofiel toe te voegen. Met **Selectie afspelen** kun je dat exacte
tijdsbereik eerst in de originele audio beluisteren. Analyse-items kunnen
afzonderlijk, als selectie of gezamenlijk worden verwijderd.

Analyse-audio wordt standaard zeven dagen bewaard, met daarnaast een globale
limiet van 2 GiB. Beide waarden zijn via **Instellingen** aanpasbaar en worden
direct toegepast; bij overschrijding worden de oudste opnamen eerst verwijderd.
Deze tijdelijke WAV's zijn uitgesloten van Home Assistant App-backups.

## Kalibratie

De pagina **Kalibratie** vergelijkt actieve enrollment-samples met samples van dezelfde en andere profielen. Er zijn meerdere samples en minstens twee verschillende speakers nodig. Het advies weegt een verkeerde persoonsmatch zwaarder dan een gemiste herkenning. De voorgestelde drempel en marge worden pas actief nadat je expliciet op **Toepassen** klikt; resetten herstelt de ingestelde basisdrempel.

## Diagnostische entiteiten

De hoofdentry maakt twee diagnose-sensoren:

- `sensor.speaker_recognition_laatste_herkenning`
- `sensor.speaker_recognition_laatste_gesprekscontext`

Ze tonen onder andere recording-id, speaker/person, confidence, marge, drempel, scores, timings, extractiestatus, blokkering en of persoonscontext aan de vervolgagent is aangeboden. Bij meerdere sprekers krijgt **Laatste herkenning** de toestand `multiple_speakers` en de attributen `speaker_count`, `speakers`, `speaker_names` en `person_entity_ids`. **Laatste gesprekscontext** krijgt eveneens `multiple_speakers` wanneer die lijst aan de conversation-agent is aangeboden. De bestaande entity-id's en enkelvoudige attributen blijven compatibel. Elke sensor wist zijn toestand dertig seconden na de laatste eigen update. `forwarded: true` betekent dat de integratie de context aan de agent heeft aangeboden; het garandeert niet dat een externe LLM die inhoud gebruikt.

## App-instellingen en API

- `log_level`: detailniveau van het App-logboek.
- `recognition_threshold`: basisdrempel voor een bekende speaker; standaard `0.65`.
- `max_audio_seconds`: maximale audioduur per verzoek.
- `api_token`: optionele bearer-token voor directe toegang buiten Ingress.

Poort `8099/tcp` staat standaard niet open. Bij directe toegang stuur je `Authorization: Bearer <api_token>`. Audio in JSON is base64-gecodeerde little-endian signed 16-bit mono PCM met een expliciete sample-rate.

Belangrijkste routes:

- `GET /api/speakers`, `POST /api/enroll` en profiel/sample-routes;
- `POST /api/recognize` voor een vluchtige compatibiliteitstest;
- `POST /api/analyze` en `/api/analysis/*` voor opgeslagen diagnose;
- `POST /api/analysis/{id}/process` om asynchroon ruis te onderdrukken;
- `GET /api/analysis/{id}/audio?variant=original|denoised`;
- `GET/PATCH /api/pipeline-policy`;
- `GET/POST/DELETE /api/calibration`;
- routes voor personen, Voice-satellieten en eenmalige Voice-opnamen.

## Privacy, backups en herstel

Stemprofielen, embeddings en enrollment-WAV's zijn biometrische gegevens en blijven lokaal in `/data`. Enrollment-WAV's en profielmetadata worden meegenomen in een koude App-backup. Tijdelijke analyse-WAV's onder `/data/analysis` niet. Na herstel verwijdert de App eventuele analyse-indexregels waarvoor geen audio meer bestaat.

Iedere gebruiker met beheerrechten voor deze App kan opgeslagen stemopnamen beluisteren of verwijderen. Publiceer poort 8099 alleen wanneer dit noodzakelijk is en gebruik dan een sterk token. Gebruik speakerherkenning nooit als authenticatiefactor of als basis om Home Assistant-rechten te verhogen.

## Bekende beperkingen

- Alleen `amd64` is ondersteund.
- Verhoog de Home Assistant-VM bij voorkeur naar 6 GB RAM. De release-smoketest begrenst de volledige container op 2 GB.
- De reproduceerbare DeepFilterNet2-metingen staan in [MODEL_BENCHMARK.md](MODEL_BENCHMARK.md).
- Browsermicrofoon vereist browsertoestemming en ondersteuning in het Ingress-frame; upload blijft beschikbaar.
- Voice-enrollment vereist de Speaker Recognition STT-proxy in de pipeline van het Voice-apparaat.
- Stemherkenning blijft probabilistisch en kan bij ruis, galm, ziekte of overlappende stemmen fouten maken.
- Meerdere sprekers worden alleen gemeld wanneer verschillende bekende stemmen overtuigend in afzonderlijke, niet-overlappende spraakregio's winnen; gelijktijdige overlap blijft één gemengde embedding.
