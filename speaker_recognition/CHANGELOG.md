# Changelog

## 2.4.0

- Verschillende bekende stemmen in niet-overlappende spraakregio's leveren nu de expliciete uitkomst `multiple_speakers`; de detectie gebruikt de bestaande kandidaatregio's en vereist geen apart diarization-model.
- De Home Assistant-diagnosesensoren en het detectie-event bevatten speakerlijsten, persoonkoppelingen, confidence en beste tijdsegmenten.
- De conversation-proxy geeft alle overtuigend herkende sprekers als niet-vertrouwde context aan de LLM door en waarschuwt dat losse woorden niet aan één persoon mogen worden toegeschreven.
- De blokkeerpolicy laat meerdere bekende sprekers door, maar blijft onbekende, ambigue en ongeldige resultaten blokkeren.

## 2.3.0

- Bestaande analyse-opnamen kunnen opnieuw worden herkend met de actuele stemprofielen, drempel, scoremarge en kalibratie zonder transcript, audioverwerking of conversation-historie te wijzigen.
- De actuele persoonkoppeling van een opnieuw herkende speaker wordt apart getoond van de historische conversation-context.
- De geselecteerde tijdsrange voor enrollment kan vóór het toevoegen aan een profiel exact worden afgespeeld en gestopt.

## 2.2.0

- Experimentele true-stateful Pipecat/DeepFilterNet3-verwerking toegevoegd achter de expliciete `audio_processing_backend: df3_streaming`-optie; `df2_batch` blijft standaard.
- De 16 → 48 → 16 kHz-keten verwerkt audio tijdens ontvangst en voert SOXR-buffers, een gedeeltelijke hop en model-lookahead expliciet af zonder de eindtijdlijn te verkorten.
- DF3-fouten en kwaliteitsafkeur vallen per aanvraag terug op de resident DeepFilterNet2/PyTorch-route.
- Tract 0.21.17, Pipecat 1.6.0, DeepFilterLib 0.5.6 en het checksum-gecontroleerde upstream DF3-archief zijn vastgepind; rekenbibliotheken gebruiken één thread.
- Een nieuwe pagina Instellingen beheert backend, pipelinebeleid en analyse-opslag zonder toegang tot `config.yaml`.
- Bestaande analyse-WAV's kunnen per uitvoering met DF2 of stateful DF3 worden verwerkt; de werkelijk gebruikte backend en drainmetingen worden bewaard.
- Ruisonderdrukte varianten en alleen hun verwerkingsmetingen kunnen veilig worden gewist en daarna opnieuw worden opgebouwd.

## 2.1.2

- DeepFilterNet2 wordt bij het starten van de add-on opgewarmd en blijft daarna resident, zodat de eerste aanvraag na inactiviteit geen koude modelstart meer krijgt.
- Denoise-metingen worden daardoor vanaf de eerste gebruikersaanvraag als warme, onderling vergelijkbare verwerking geregistreerd.

## 2.1.1

- SpEx+ en doelstemisolatie zijn volledig uit de runtime en image verwijderd; optionele audiobewerking bestaat voortaan alleen uit DeepFilterNet2-ruisonderdrukking.
- Handmatige ruisonderdrukking bewaart de oorspronkelijke pipelinetijd en toont de totale geschatte pipeline inclusief de extra audiobewerking.
- Herverwerking van een bestaande opname telt eerdere verwerkingstijd niet opnieuw op.
- Koude modelstarts krijgen aparte laad- en cold-startmetingen. Alleen runs waarbij DeepFilterNet2 al geladen was leveren vergelijkbare `denoise_ms` en `audio_processing_ms`.

## 2.1.0

- Echte lokale ruisonderdrukking met DeepFilterNet2 en enrollment-gestuurde doelstemisolatie met SpEx+.
- Analyse bewaart en vergelijkt originele, ruisonderdrukte en geïsoleerde audio met kwaliteitsmetingen, fasestatussen en fallbacks.
- De experimentele modus vóór STT kiest veilig `isolated → denoised → original` binnen een harde tijdslimiet.
- Audiomodellen en gewichten zijn vastgepind, checksum-gecontroleerd en volledig offline in de add-onimage opgenomen.
- De oude VAD-knipactie blijft alleen als legacy-audio beschikbaar; nieuwe verwerking behoudt de volledige tijdlijn.
- Live STT heeft voorrang op Analyse-taken, gebruikt één totale tijdsgrens en valt veilig terug bij geheugen-, kwaliteits- of modelproblemen.
- Clips tot 120 seconden worden ondersteund; boven dertig seconden gebruikt SpEx+ overlappende blokken met crossfade.
- De CI bouwt de echte image en voert de volledige modelketen zonder netwerk onder een 2 GB-geheugenlimiet uit.

## 2.0.2

- Voice-enrollment start nu zonder vooraankondigingsgong. Dit voorkomt dat Home Assistant Voice na “Spreek nu” terugkeert naar `idle` zonder de microfoon te openen.
- De enrollment-API ondersteunt daarnaast een fysieke-knopfallback voor satellieten die remote start niet ondersteunen.
- De automatische reset van diagnostische sensoren blijft op de Home Assistant-eventloop en voldoet daarmee aan de strengere thread-safetycontrole.

## 2.0.1

- Voice-enrollment claimt de STT-stream nu atomisch met de satelliet-ID die Home Assistant lokaal vastlegt zodra de stream start. Hierdoor kan de korte overgang van `listening` naar `processing` of `idle` de opname niet meer voortijdig afbreken.

## 2.0.0

- Multi-window- en VAD-herkenning toegevoegd met confidence, minimale marge, segmenten en verwerkingstijden.
- Globale pipeline-policy toegevoegd voor onbekende-speakerblokkering en optionele experimentele speaker-extractie.
- Alle gewone pipeline- en handmatige testopnamen krijgen zeven dagen analysehistorie met transcript, scores, timings en afspeelbare WAV-audio.
- Nieuwe Analyse-pagina met zoeken/filteren, bulkverwijdering, offline extractievergelijking en golfvormselectie voor promotie naar een profiel.
- Enrollment-WAV's worden permanent bewaard en zijn per profiel afspeelbaar, downloadbaar, activeerbaar, deactiveerbaar en verwijderbaar.
- Profielvervanging archiveert oude samples; profielverwijdering vraagt expliciet of audio wordt gewist of bewaard.
- Kalibratiewizard toegevoegd met een conservatief advies en expliciete toepas/reset-stap.
- Diagnose-sensoren uitgebreid met recording-id, marge, drempelbron, segment, timings, extractie/fallback en blokkering.
- Tijdelijke analyse-audio is beperkt tot zeven dagen en 2 GiB, oudste eerst, en uitgesloten van App-backups.

## 1.3.5

- **Test een fragment** opent voortaan een eigen modal met audio-upload, browsermicrofoon en Home Assistant Voice.
- Testfragmenten kunnen vóór herkenning worden teruggeluisterd, vervangen of verwijderd.
- Actieve browser- en Voice-opnames worden bij annuleren veilig afgebroken.

## 1.3.4

- **Laatste herkenning** en **Laatste gesprekscontext** worden 30 seconden na hun eigen laatste update automatisch gewist.
- Een nieuwe update vervangt de lopende reset-timer; timers worden bij het unloaden van de integratie opgeruimd.

## 1.3.3

- Vijftig eenvoudige Nederlandse voorbeeldteksten toegevoegd voor gevarieerde enrollment-opnames.
- De modal kiest zonder directe herhaling een willekeurige tekst en toont na iedere nieuwe opname automatisch een volgende.
- Een knop **Andere tekst** maakt handmatig wisselen mogelijk; voorlezen blijft optioneel.

## 1.3.2

- De Voice-enrollmentprompt is verkort naar “Spreek nu.”, zodat de luisterfase de gesproken instructie niet voortijdig afbreekt.

## 1.3.1

- Diagnostische Home Assistant-sensor voor de laatste speakerherkenning toegevoegd.
- Aparte sensor toont expliciet of een gekoppelde `person.*`-context aan de vervolg-LLM is doorgegeven.
- Confidence, matchstatus, speaker/person, satelliet, STT-bron, scores en tijdstip zijn als attributen zichtbaar.
- Enrollment-modal kan altijd worden geannuleerd en breekt actieve browser- en Voice-opnames netjes af.
- Opgenomen en geüploade fragmenten kunnen vóór opslaan worden teruggeluisterd.
- Eenvoudige optionele voorbeeldtekst toegevoegd voor een bruikbaar spraakfragment.
- Voice-enrollment sluit af met een korte bevestiging zodat het apparaat terugkeert naar de idle-status.
- Een voltooid Voice-fragment kan niet meer door een late browser-cleanup naar `cancelled` veranderen.

## 1.3.0

- Concept 6 toegevoegd als App-icoon en lokaal Home Assistant integration-brand.
- Backend-URL en companion-token zijn zichtbaar configureerbaar en worden vóór opslaan gevalideerd.
- Selecteerbare conversation-proxy rond een bestaande Home Assistant conversation-agent toegevoegd.
- Optionele enrollmentkoppeling met een Home Assistant `person.*`-entiteit toegevoegd.
- Herkenningsresultaten zijn kortlevend, satellietgebonden en worden maximaal één keer voor personalisatie gebruikt.
- De oorspronkelijke Home Assistant-gebruiker en rechtencontext worden nooit door een stemmatch gewijzigd.

## 1.2.0

- Enrollment via een bestaand Home Assistant Voice-/Assist Satellite-apparaat toegevoegd.
- De App ontdekt beschikbare satellieten en start een veilige STT-only `ask_question`-opname.
- Enrollmentspraak bereikt nooit de conversation- of intent-laag en kan dus geen commando uitvoeren.
- Opnames worden alleen geaccepteerd als de gekozen satelliet de enige luisterende satelliet is.
- Sessies zijn atomisch, begrensd en verlopen automatisch; ruwe opname blijft alleen kort in het geheugen.

## 1.1.0

- Meegeleverde Home Assistant custom integration wordt automatisch geïnstalleerd en bijgewerkt.
- Supervisor-discovery laat de actieve App als ontdekt verschijnen.
- Beveiligde App-koppeling, STT-proxy en herkenningsevent zonder gebruikersrechten te wijzigen.
- Oorspronkelijke WAV/PCM-, samplerate-, gedeelde-resultaat- en API-problemen opgelost.

## 1.0.0

- Eerste Timminater-release op basis van EuleMitKeule/speaker-recognition.
- Ingress-GUI voor upload, microfoon, lijst, verwijderen en herkenningstest.
- Persistente profielen die na restart automatisch worden geladen.
- Veilige profiel-ID's, begrensde PCM-input en atomische opslag.
- Multi-sample aggregatie, append/replace en configureerbare unknown-drempel.
- Ingress-authenticatie en optionele bearer-token voor directe API-toegang.
