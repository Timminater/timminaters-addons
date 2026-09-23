# Speaker Recognition-documentatie

## Companion-integratie

De App installeert of actualiseert bij het starten de meegeleverde `speaker_recognition` custom integration onder `/homeassistant/custom_components` en meldt de backend aan via Supervisor-discovery. Herstart Home Assistant Core na de eerste installatie. Daarna verschijnt de App onder **Instellingen > Apparaten & diensten > Ontdekt**.

Een al aanwezige integratiemap die niet door deze App wordt beheerd, wordt eerst bewaard als `speaker_recognition.pre-app-backup`. Het verwijderen van de App herstelt die map niet automatisch.

Voeg de integratie daarna nogmaals toe voor:

1. een **STT-proxy** rond de STT-engine van de Assist-pipeline; en
2. optioneel een **conversation-proxy** rond de bestaande conversation-agent.

De STT-proxy laat dezelfde audiostream herkennen en stuurt hem volgens de globale policy door naar STT. De conversation-proxy geeft de bestaande conversation-agent en oorspronkelijke Home Assistant `Context` door. De integratie voegt alleen persoonscontext toe als STT en gesprek dezelfde expliciete pipeline-run-ID leveren. De huidige Home Assistant `SpeechMetadata` en `ConversationInput` delen die ID niet; daarom wordt op huidige versies geen automatische persoonscontext doorgestuurd. Dit is fail-closed: alleen dezelfde satelliet en een kort tijdsinterval zijn onvoldoende om een identiteit aan een gesprek te koppelen. De herkenningssensor kan het STT-resultaat nog tonen, maar `forwarded` blijft zonder veilige correlatie uit. Een stemmatch verandert nooit `Context.user_id`, authenticatie of rechten.

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

### Beoordelen, labels en opgeslagen opnamen

De pagina **Beoordelen** verzamelt onbekende, ambigue en mislukte of geblokkeerde
herkenningen, plus opnamen die je handmatig voor beoordeling markeert. De inbox
bewaart de analysemetadata volgens de normale analysehistorie. Een opname kan
alleen worden afgespeeld zolang de bijbehorende originele WAV volgens het
ingestelde bewaarbeleid nog bestaat. Bij **Niets bewaren** kan een item dus wel
met metadata in de inbox staan, maar zonder audio-speler.

Vanuit een item kun je de juiste spreker of **Onbekend** als handmatige waarheid
vastleggen, het item negeren, audio aan een bestaand profiel toevoegen of een
nieuw profiel maken. Een handmatig label wijzigt alleen de beoordelingsmetadata:
het traint of wijzigt nooit vanzelf een stemprofiel. Audio toevoegen aan een
profiel blijft een afzonderlijke, expliciete actie met fragmentselectie en de
gebruikelijke kwaliteitscontrole.

Bij **Nieuw stemprofiel** kun je ook een bestaande opname opzoeken in de
opnamekiezer. Alleen opnamen waarvan de originele audio nog bestaat zijn
selecteerbaar. Je kunt de opname beluisteren, een fragment kiezen en de
kwaliteitsfeedback bekijken voordat je dat fragment als enrollment bevestigt.
Dezelfde route is beschikbaar via **Analyse → opnamedetail → Audio gebruiken**.
Een opname die als bron voor enrollment is gebruikt, telt niet mee als
onafhankelijke proefopname voor dat profiel.

### Gastprofielen, samenvoegen en apparaatkwaliteit

Een **gastprofiel** is een tijdelijk stemprofiel zonder Home Assistant-
persoonskoppeling. De standaard einddatum is dertig dagen na registratie; je
kunt een eigen einddatum kiezen of **Nooit** instellen. Herkenning als gast
verlengt de einddatum niet. Op de einddatum worden het gastprofiel en de
bijbehorende enrollment-opnamen automatisch verwijderd. Namen en
speakerverwijzingen van de gast worden uit historische analysedata gewist of
geanonimiseerd; eventuele analyse-WAV's volgen afzonderlijk het ingestelde
analyse-bewaarbeleid.

Met **Samenvoegen** kies je een bronprofiel en een bestemmingsprofiel. De
bestemming behoudt zijn naam en eventuele persoonskoppeling; actieve samples
worden onder die bestemming opnieuw berekend. Oude herkenningsruns blijven als
historische resultaten staan. Samenvoegen wordt geweigerd met een begrijpelijke
melding als een actieve WAV ontbreekt. Bij een opslagfout wordt de wijziging
teruggedraaid zodat sample-eigendom en profielen niet half zijn bijgewerkt.

De pagina **Apparaten** toont per Voice-apparaat het aantal opnamen en
herkenningen, onbekende/ambigue stemmen, kwaliteitsproblemen en verwerkingstijd
(gemiddeld en p95) over een gekozen periode. Dit zijn diagnosecijfers uit de
opgeslagen analysehistorie; ontbrekende audio sluit een item niet uit de telling.

### Proefstand en experimentele sprekerstijdlijn

Op de pagina **Proefstand** kun je handmatig gelabelde opnamen opnieuw
beoordelen met een kandidaatdrempel en scoremarge. De proefstand verandert de
productiedrempel, marge, kalibratie en profielen niet. Opnamen zonder bewaarde
WAV, zonder handmatig label, met verouderd profiel-label of eerder gebruikt
voor enrollment worden uitgesloten. Een alternatief model is pas beschikbaar
als er een aparte experimentele modelroute is geïmplementeerd en technisch
gevalideerd; momenteel wordt alleen de bestaande stemencoder aangeboden.
Resultaten op een kleine of niet-onafhankelijke verzameling zijn geen
nauwkeurigheidsclaim.

In **Analyse** kun je voor een bewaarde opname vrijwillig een **experimentele
sprekerstijdlijn** starten. Deze offline analyse gebruikt de bestaande
stemencoder en groepeert voldoende zekere, opeenvolgende spraakvensters als een
bekende spreker; onzekere of overlappende vensters krijgen geen geforceerde
naam. De tijdlijn levert geen woordtiming uit het transcript. Ze wijzigt de
live Assist-route en het gesprekstranscript niet en kent geen Home Assistant-
persoonscontext of rechten toe. De functie blijft experimenteel totdat echte,
handmatig gelabelde opnamen de kwaliteit op een onafhankelijke testset
onderbouwen.

## Kalibratie

De pagina **Kalibratie** vergelijkt actieve enrollment-samples met samples van dezelfde en andere profielen. Er zijn meerdere samples en minstens twee verschillende speakers nodig. Het advies weegt een verkeerde persoonsmatch zwaarder dan een gemiste herkenning. De voorgestelde drempel en marge worden pas actief nadat je expliciet op **Toepassen** klikt; resetten herstelt de ingestelde basisdrempel.

## Diagnostische entiteiten

De hoofdentry maakt twee diagnose-sensoren:

- `sensor.speaker_recognition_laatste_herkenning`
- `sensor.speaker_recognition_laatste_gesprekscontext`

Ze tonen onder andere recording-id, speaker/person, confidence, marge, drempel, scores, timings, extractiestatus, blokkering en de status van persoonscontext. Bij meerdere sprekers krijgt **Laatste herkenning** de toestand `multiple_speakers` en de attributen `speaker_count`, `speakers`, `speaker_names` en `person_entity_ids`. **Laatste gesprekscontext** meldt of context aan de conversation-agent is aangeboden. Op huidige Home Assistant-versies ontbreekt een gedeelde pipeline-run-ID, dus wordt geen persoonscontext aangeboden en is `forwarded` niet `true`. De bestaande entity-id's en enkelvoudige attributen blijven compatibel. Elke sensor wist zijn toestand dertig seconden na de laatste eigen update. Alleen wanneer beide pipelinecomponenten later dezelfde expliciete run-ID leveren, betekent `forwarded: true` dat de integratie context heeft aangeboden; dit zegt niet dat een externe LLM die inhoud gebruikt.

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
- `GET /api/review-inbox` en `PATCH /api/analysis/{id}/review` voor inboxstatus en handmatige waarheid;
- `GET /api/devices/quality` voor statistieken per Voice-apparaat;
- `POST /api/experiments/preview` en `GET /api/experiments/{job_id}` voor een proefrun;
- `POST/GET /api/analysis/{id}/diarization` voor de experimentele offline sprekerstijdlijn;
- `GET/PATCH /api/pipeline-policy`;
- `GET/POST/DELETE /api/calibration`;
- routes voor personen, Voice-satellieten en eenmalige Voice-opnamen.

### API-versie 2, audio en herkenningsruns

De companion-integratie vraagt `GET /api/info` op en gebruikt de
capabilities uit dit document om transport te kiezen. Het antwoord bevat
`api_version`, App-versie, beschikbare `capabilities`, geconfigureerde
ruisonderdrukkingsbackend, componentversies en limieten. De huidige API meldt
onder andere `analysis_v2`, `binary_analyze`, `multipart_enroll`,
`df3_streaming`, `processing_status`, `review_inbox`, `guest_profiles`,
`profile_merge`, `device_quality`, `experiment_preview` en
`offline_diarization_experimental`. Een capability betekent dat het contract
beschikbaar is; het bewijst op zichzelf geen kwaliteits- of snelheidswinst van
een experimentele backend.

Voor analyse ondersteunt `POST /api/analyze-binary` ruwe mono PCM met
`Content-Type: application/octet-stream` en headers `X-Sample-Rate`,
`X-Channels: 1` en `X-Audio-Format: pcm_s16le`. `source`, `satellite_id`,
`stt_entity_id` en `extraction_mode` zijn queryparameters. Het sampleformaat
is little-endian signed 16-bit PCM; samplefrequentie moet voldoen aan het
audio-contract van de App (8–48 kHz) en de duur aan `max_audio_seconds`.
Onjuiste kanalen, encoding, PCM-uitlijning of lengtes worden afgewezen. De
companion kiest deze route alleen als `/api/info` `binary_analyze` meldt; bij
oudere backends blijft hij JSON/base64 gebruiken via `POST /api/analyze`.

Voor directe enrollment ondersteunt `POST /api/enroll-multipart` herhaalde
multipart-bestandsvelden met naam `recordings`; elk deel bevat een clip als
`application/octet-stream` of `audio/pcm`. Queryparameters zijn
`speaker_name`, optioneel `replace_existing` en `person_entity_id`. Headers
zijn `X-Sample-Rate`, `X-Channels: 1` en `X-Audio-Format: pcm_s16le`. De
server accepteert maximaal acht clips, ieder maximaal 30 seconden en 8 MiB,
met een totaal multipart-verzoek tot 40 MiB. Namen van aangeleverde bestanden
worden niet als paden gebruikt. De huidige web-enrollment verstuurt nog de
bestaande JSON/base64-route `/api/enroll`; multipart is beschikbaar voor
clients die dit expliciet implementeren.

`POST /api/audio-quality` geeft vooraf feedback op `{ "audio": ...,
"purpose": "registration" }` en retourneert een beslissing, meetwaarden en
flags. De registratiepoort weigert bij te weinig gemeten actieve spraak (minder
dan 0,6 seconde of 5% van de opname) en sterke clipping (minstens 1% van de
samples raakt de clipgrens). Andere kwaliteitsadviezen, zoals lichte clipping
of een hoog geschat achtergrondniveau, vragen eerst om beoordeling. Zonder
expliciete acceptatie geeft de registratiepoort HTTP 409
`registration_quality_review_required`; `accept_quality_warnings: true` laat
de gebruiker na die beoordeling doorgaan. Harde afwijzingen geven HTTP 422
`registration_audio_rejected` en kunnen niet met die vlag worden omzeild.

Dezelfde poort geldt voor JSON `/api/enroll` (veld
`accept_quality_warnings`), multipart `/api/enroll-multipart` (queryparameter
`accept_quality_warnings=true`) en het promoveren van een analysefragment
(`/api/analysis/{id}/promote`, JSON-veld `accept_quality_warnings`). De
metingen zijn gebaseerd op frame-energie en clipping; dit is een eenvoudige
energieheuristiek, geen spraakherkenner of VAD-model. Een opname kan dus
actieve spraak lijken te bevatten zonder dat er verstaanbare spraak is. Deze
feedback bewijst nooit wie er spreekt en mag niet als identiteitsbewijs of
toegangscontrole worden gebruikt.

De instelling **Analyse-audio bewaren** kent drie waarden:

- **Niets bewaren** (`none`): analysemetadata en herkenningsresultaat kunnen
  nog in de historie staan, maar analyse-WAV's worden verwijderd of niet
  aangemaakt. Audio kan dan niet opnieuw worden afgespeeld of geanalyseerd.
- **Alleen problematische opnamen** (`errors`): originele audio wordt bewaard
  voor onzekere, onbekende/geblokkeerde of mislukte analyses; een geslaagde
  bekende match wordt na verwerking verwijderd.
- **Alle opnamen tijdelijk bewaren** (`all`): analyse-audio wordt volgens de
  ingestelde retentiedagen en opslaglimiet opgeschoond.

Dit beleid betreft de lokale analyse-audio van de App. Het verwijdert geen
transcripties of context uit Home Assistant-geschiedenis en past geen reeds
gemaakte Home Assistant-back-ups aan. Enrollment-audio en -profielen hebben
een eigen bewaarbeleid. `/api/recognize` blijft een vluchtige herkenningsroute
en maakt geen opname in de analysehistorie.

`GET /api/storage` geeft de opslagomvang per categorie terug (originele en
afgeleide analyse-WAV's, actieve en gearchiveerde enrollment-WAV's,
niet-geïndexeerde WAV-bytes) en een orphan-scan. `orphans` onderscheidt
verwijzingen naar ontbrekende bestanden van WAV-bestanden zonder indexrecord.
De scan verwijdert niets. `POST /api/storage/orphans/delete` verwijdert alleen
door de client expliciet geselecteerde `paths` (1–100 per aanvraag), en alleen
als elk pad bij de hercontrole nog steeds een niet-geïndexeerde WAV binnen de
eigen analyse- of enrollmentopslag is. Hiermee kun je een gevonden orphan
gericht opruimen; de route doet geen brede automatische bestandsverwijdering.

Bij profielverwijdering kan permanente enrollment-audio worden gearchiveerd
in plaats van gewist. `GET /api/archived-samples` toont gearchiveerde of
inactieve samples met metadata maar zonder bestandspad; `DELETE
/api/archived-samples/{sample_id}` verwijdert één geselecteerde sample en de
bijbehorende WAV definitief uit de App-opslag. De UI toont geen profiel- of
persoonsnaam bij deze archiefitems.

`GET /api/diagnostics` levert een standaard privacyvriendelijke JSON-export
met schemaversie, API/componentinformatie, gereedstatus, opslagcategorieën en
pipelinebeleid. De export bevat geen token, audio, transcript of persoonsnaam.
Opslag- en opruimroutes beheren uitsluitend bestanden en indexen van deze App;
ze verwijderen of wijzigen geen Home Assistant-geschiedenis of bestaande
back-ups. Back-ups kunnen eerder opgeslagen gegevens blijven bevatten.

Iedere voltooide analyse en heranalyse voegt een herkenningsrun toe met een
vastgelegde profielrevisie en de bij die run gebruikte uitkomst, scores,
drempel, marge en relevante instellingen. Runs zijn append-only en kunnen
worden opgehaald met `GET /api/analysis/{recording_id}/runs`. De algemene
analysekaart toont het nieuwste resultaat; opnieuw analyseren werkt die
samenvatting bij, maar vervangt de oudere run niet. Runhistorie bevat geen
audiofragmenten. Een opname verwijderen verwijdert ook de bijbehorende
recognition runs.

### Validatiegrenzen

De implementatie gebruikt Resemblyzer op PyTorch CPU als herkenningsbasis.
Er is nog geen onafhankelijke, gelabelde testset die Resemblyzer met CAM++,
ReDimNet2, ERes2NetV2 of ECAPA-TDNN vergelijkt. De kandidaatmodellen zijn dus
niet gevalideerd voor deze installatie en er is nog geen onderbouwde snelle /
nauwkeurige modelkeuze.

DF3 blijft experimenteel. De bestaande streaming-, synthetische-audio- en
lokale smokechecks zijn geen onafhankelijke validatie op echte Nederlandse
Voice-opnamen. De volledige Linux-image-, Nederlandse STT-, laatste-woord-,
gelijktijdigheids- en Home Assistant-VM-evaluatie uit
[DF3_STREAMING_VALIDATION.md](DF3_STREAMING_VALIDATION.md) is nog vereist
voordat DF3 als standaardroute kan gelden. Ook CPU-thread- en GPU/OpenVINO-
resultaten zijn niet op een onafhankelijke testset aangetoond; er wordt geen
hardwareversnelling of kwaliteitsverbetering geclaimd.

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
