# Speaker Recognition-documentatie

## Companion-integratie

De App installeert of actualiseert bij het starten de meegeleverde `speaker_recognition` custom integration onder `/homeassistant/custom_components` en meldt de backend aan via Supervisor-discovery. Herstart Home Assistant Core na de eerste installatie. Daarna verschijnt de App onder **Instellingen > Apparaten & diensten > Ontdekt**.

Een al aanwezige integratiemap die niet door deze App wordt beheerd, wordt eerst bewaard als `speaker_recognition.pre-app-backup`. Het verwijderen van de App herstelt die map niet automatisch.

Voeg de integratie daarna nogmaals toe voor:

1. een **STT-proxy** rond de STT-engine van de Assist-pipeline; en
2. optioneel een **conversation-proxy** rond de bestaande conversation-agent.

De STT-proxy laat dezelfde audiostream herkennen en stuurt hem volgens de globale policy door naar STT. De conversation-proxy kan een verse, exact aan dezelfde Voice-satelliet gekoppelde stemmatch als niet-vertrouwde personalisatiecontext aanbieden. Tekst, taal, conversation-id, device/satellite-id en vooral het oorspronkelijke Home Assistant `Context` blijven behouden. Een stemmatch verandert nooit `Context.user_id`, authenticatie of rechten.

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

De herkenner beoordeelt de volledige uiting, spraakregio's en overlappende tijdvensters. De hoogste overeenkomst bepaalt de kandidaat. Een resultaat is alleen een match als zowel de confidence-drempel als de minimale marge ten opzichte van de tweede kandidaat wordt gehaald.

De globale policy in de webinterface bevat:

- **Onbekende speaker toestaan** (standaard): STT en conversation blijven werken wanneer niemand wordt herkend.
- **Onbekende speaker blokkeren**: een onbekende of ambigue uiting stopt vóór de conversation-agent. Gebruik dit niet als beveiligingsmiddel voor sloten, alarmen of andere gevoelige acties.
- **Extractie uit** (standaard): STT ontvangt de oorspronkelijke audio.
- **Alleen vergelijken**: de App maakt en bewaart waar mogelijk ook audio met alleen regio's van de herkende speaker; STT ontvangt nog steeds het origineel.
- **Vóór STT** (experimenteel): herkenning vindt eerst plaats en STT krijgt de geëxtraheerde audio. Als extractie mislukt, valt de proxy terug op het origineel.

Wanneer de backend niet bereikbaar is, blijft de normale `allow`-policy fail-open. Een actief bekende `block`-policy faalt gesloten.

## Analyse

De pagina **Analyse** bewaart gewone Assist-pipeline-opnamen en opnamen van **Test een fragment**. De generieke externe `/api/recognize`-route blijft vluchtig en wordt niet gelogd.

Per item zijn, voor zover beschikbaar, zichtbaar:

- origineel en geëxtraheerd WAV-fragment;
- transcript en bron/satelliet;
- match, confidence, drempel, marge en alle profiel-scores;
- gebruikte segmenten en het beste tijdvenster;
- herkennings-, extractie-, STT- en totale verwerkingstijd;
- extractiemodus, fallback, blokkering en doorgifte aan de conversation-agent.

Je kunt op de golfvorm een begin- en eindpunt kiezen en dat deel toevoegen aan een bestaand profiel of als nieuw profiel opslaan. Analyse-items kunnen afzonderlijk, als selectie of gezamenlijk worden verwijderd.

Analyse-audio wordt standaard zeven dagen bewaard, met daarnaast een globale limiet van 2 GiB. Bij overschrijding worden de oudste opnamen eerst verwijderd. Deze tijdelijke WAV's zijn uitgesloten van Home Assistant App-backups.

## Kalibratie

De pagina **Kalibratie** vergelijkt actieve enrollment-samples met samples van dezelfde en andere profielen. Er zijn meerdere samples en minstens twee verschillende speakers nodig. Het advies weegt een verkeerde persoonsmatch zwaarder dan een gemiste herkenning. De voorgestelde drempel en marge worden pas actief nadat je expliciet op **Toepassen** klikt; resetten herstelt de ingestelde basisdrempel.

## Diagnostische entiteiten

De hoofdentry maakt twee diagnose-sensoren:

- `sensor.speaker_recognition_laatste_herkenning`
- `sensor.speaker_recognition_laatste_gesprekscontext`

Ze tonen onder andere recording-id, speaker/person, confidence, marge, drempel, scores, timings, extractiestatus, blokkering en of persoonscontext aan de vervolgagent is aangeboden. Elke sensor wist zijn toestand dertig seconden na de laatste eigen update. `forwarded: true` betekent dat de integratie de context aan de agent heeft aangeboden; het garandeert niet dat een externe LLM die inhoud gebruikt.

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
- `GET/PATCH /api/pipeline-policy`;
- `GET/POST/DELETE /api/calibration`;
- routes voor personen, Voice-satellieten en eenmalige Voice-opnamen.

## Privacy, backups en herstel

Stemprofielen, embeddings en enrollment-WAV's zijn biometrische gegevens en blijven lokaal in `/data`. Enrollment-WAV's en profielmetadata worden meegenomen in een koude App-backup. Tijdelijke analyse-WAV's onder `/data/analysis` niet. Na herstel verwijdert de App eventuele analyse-indexregels waarvoor geen audio meer bestaat.

Iedere gebruiker met beheerrechten voor deze App kan opgeslagen stemopnamen beluisteren of verwijderen. Publiceer poort 8099 alleen wanneer dit noodzakelijk is en gebruik dan een sterk token. Gebruik speakerherkenning nooit als authenticatiefactor of als basis om Home Assistant-rechten te verhogen.

## Bekende beperkingen

- Alleen `amd64` is ondersteund.
- Browsermicrofoon vereist browsertoestemming en ondersteuning in het Ingress-frame; upload blijft beschikbaar.
- Voice-enrollment vereist de Speaker Recognition STT-proxy in de pipeline van het Voice-apparaat.
- Stemherkenning blijft probabilistisch en kan bij ruis, galm, ziekte of overlappende stemmen fouten maken.
