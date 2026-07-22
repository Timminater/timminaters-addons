# Speaker Recognition-documentatie

## Companion-integratie

Bij elke start installeert of actualiseert de App de meegeleverde `speaker_recognition`
custom integration en meldt hij zich aan via Supervisor-discovery. Herstart Home Assistant
Core na de eerste App-start eenmaal, omdat nieuwe custom components alleen tijdens een
Core-start worden ingelezen. Bevestig daarna de gevonden Speaker Recognition App bij
**Instellingen > Apparaten & diensten**.

Voor deze automatische installatie krijgt de App, zoals in het companion-integrationpatroon,
schrijftoegang tot de Home Assistant-configuratiemap. Een bestaande, niet door de App beheerde
integratie wordt eerst bewaard als
`custom_components/speaker_recognition.pre-app-backup`; verwijdering van de App herstelt die
backup niet automatisch.

Voeg de integratie daarna nogmaals toe om een STT- of conversation-proxy te maken. Iedere herkenning vuurt
het event `speaker_recognition_detected` af met profiel, confidence en scores. Herkenning
wijzigt bewust nooit `Context.user_id` of gebruikersrechten: een stemmatch is metadata en
geen authenticatiemiddel.

### Backend en conversation-agent

De originele upstreamintegratie noemt vier configuratiestappen. In deze versie zijn die als
volgt beschikbaar:

1. De herkenningsbackend wordt automatisch via de App ontdekt. Via **Configureren** op de
   hoofdentry kun je desgewenst een andere compatibele interne URL en token instellen; de
   verbinding wordt gecontroleerd voordat de wijziging wordt opgeslagen.
2. Tijdens enrollment kan een profiel optioneel aan een Home Assistant `person.*`-entiteit
   worden gekoppeld. Dit is uitsluitend metadata voor events en personalisatie.
3. Via **Integratie toevoegen > Speaker Recognition > STT-proxy toevoegen** kies je een
   bestaande `stt.*`-entiteit.
4. Via **Conversation-proxy toevoegen** kies je een bestaande `conversation.*`-agent en een
   minimale confidence. De nieuwe proxy verschijnt zelf als selecteerbare conversation-agent.

De conversation-proxy bewaart tekst, taal, conversation-id, device/satellite-id en vooral
de oorspronkelijke Home Assistant `Context` en gebruikersrechten. Alleen een verse,
eenmalig gebruikte match van dezelfde Voice-satelliet kan de gekoppelde `person.*`-ID als
expliciet niet-vertrouwde personalisatiecontext toevoegen. De proxy mag nooit op basis van
een stemmatch extra rechten verlenen.

### Diagnose-entiteiten

De hoofdentry maakt twee diagnostische sensoren aan op het apparaat **Speaker Recognition**:

- **Laatste herkenning** toont de herkende speaker en bevat onder meer `matched`,
  `confidence`, `person_entity_id`, `satellite_id`, alle scores en het tijdstip als attributen.
- **Laatste gesprekscontext** toont de gekoppelde `person.*`-entiteit wanneer deze context
  daadwerkelijk aan de gekozen vervolgagent is aangeboden. De attributen `forwarded`,
  `reason`, `source_conversation_entity` en `minimum_confidence` maken de routering controleerbaar.

`forwarded: true` bewijst dat de integratie de persoonscontext via Home Assistants
conversation-contract aan de vervolgagent heeft aangeboden. Het kan niet garanderen dat een
externe LLM de instructie inhoudelijk volgt. Beide sensoren bewaren alleen het laatste
resultaat in het geheugen en bevatten geen audio.

## Enrollment

Open de webinterface via Home Assistant Ingress en kies **Nieuwe speaker**. Je kunt meerdere bestanden tegelijk selecteren of samples opnemen met de microfoon. Gebruik bij voorkeur:

- 2–3 samples per persoon;
- 5–30 seconden duidelijke, natuurlijke spraak per sample;
- verschillende zinnen en liefst verschillende opnamemomenten;
- zo min mogelijk muziek, echo en andere stemmen.

Kies eventueel een Home Assistant-persoon in het enrollmentvenster. Bestaande profielen
zonder koppeling blijven volledig compatibel. De koppeling wordt samen met het lokale
stemprofiel opgeslagen en wordt ook in herkenningsevents teruggegeven.

Upload werkt met audioformaten die de actieve browser kan decoderen. Microfoonopname is een progressive enhancement: `getUserMedia` vereist HTTPS, browsertoestemming en ondersteuning door de Home Assistant Ingress-iframe. Als dat niet beschikbaar is, blijft upload volledig werken.

### Opnemen via Home Assistant Voice

De GUI toont online `assist_satellite`-apparaten die een gesprek op afstand kunnen starten.
Om hun audiostream te kunnen onderscheppen:

1. Voeg Speaker Recognition nogmaals toe onder **Instellingen > Apparaten & diensten** en kies **STT-proxy toevoegen**.
2. Selecteer de STT-engine die je normale Assist-pipeline gebruikt.
3. Kies de nieuw ontstane Speaker Recognition STT-entiteit als STT-engine in de Assist-pipeline van het Voice-apparaat.
4. Kies in de enrollment-GUI het Voice-apparaat en druk op **Opnemen via Voice**.

De App gebruikt hiervoor `assist_satellite.ask_question`. Home Assistant beëindigt deze
speciale pipeline na STT; de gesproken enrollment-zin gaat niet naar de conversation-agent
of intent-laag en kan dus geen apparaat bedienen. De stream wordt alleen geaccepteerd als
het gekozen Voice-apparaat de enige satelliet met status `listening` is. Gebruik tijdens de
korte opname geen ander Voice-apparaat.

## Instellingen

- `log_level`: logniveau van de service.
- `recognition_threshold`: minimale cosine-similarity voor een bekende speaker. Onder deze waarde geeft de API “onbekend” terug. Begin met `0.65` en valideer met eigen positieve en negatieve testfragmenten.
- `max_audio_seconds`: serverlimiet per sample.
- `api_token`: bearer-token voor optionele rechtstreekse API-toegang. Zonder token accepteert de API alleen Supervisor Ingress-verzoeken.

Poort `8099/tcp` is standaard niet aan een hostpoort gekoppeld. Stel alleen een mapping in als een externe client de REST-API nodig heeft en configureer dan altijd een sterk `api_token`.

## REST-API

Ingress verzorgt authenticatie voor de GUI. Bij rechtstreekse toegang stuur je `Authorization: Bearer <api_token>`.

- `GET /health` — readiness en aantal profielen.
- `GET /api/speakers` — lijst met profielen.
- `POST /api/enroll` — append of replace van één profiel.
- `DELETE /api/speakers/{id}` — profiel verwijderen.
- `POST /api/recognize` — speaker testen.
- `GET /api/assist-satellites` — beschikbare Voice-apparaten.
- `GET /api/home-assistant-persons` — beschikbare `person.*`-entiteiten voor enrollment.
- `POST /api/satellite-enrollment` — een eenmalige Voice-opname starten.

Audio in de JSON-contracten is base64-gecodeerde, little-endian signed 16-bit mono PCM met een expliciete sample-rate. De webinterface converteert uploads en microfoonopnames automatisch naar 16 kHz.

## Privacy, backups en herstel

Stem-embeddings zijn biometrische gegevens. Ze blijven lokaal in `/data/speakers`; ruwe audio wordt niet op schijf opgeslagen. Een Voice-enrollmentfragment blijft maximaal vijf minuten in het App-geheugen beschikbaar om het profiel vanuit de GUI op te slaan en wordt daarna gewist. Home Assistant neemt `/data` mee in een App-backup. Verwijderen in de GUI verwijdert het embeddingbestand en de metadata definitief.

## Bekende beperkingen

- Alleen `amd64` is ondersteund.
- Browsermicrofoon kan per Home Assistant-/browserrelease verschillen; upload is de gegarandeerde route.
- Een volledig eerlijke herkenningstest gebruikt andere audio dan de enrollment-samples.
- Voice-enrollment vereist dat de Assist-pipeline de Speaker Recognition STT-proxy gebruikt; zonder die proxy kan de App de microfoonstream niet ontvangen.
- Speakerherkenning is probabilistisch en mag niet worden gebruikt als authenticatiefactor voor sloten, alarmen, betalingen of andere gevoelige acties.
