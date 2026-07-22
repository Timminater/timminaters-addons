# Speaker Recognition-documentatie

## Enrollment

Open de webinterface via Home Assistant Ingress en kies **Nieuwe speaker**. Je kunt meerdere bestanden tegelijk selecteren of samples opnemen met de microfoon. Gebruik bij voorkeur:

- 2–3 samples per persoon;
- 5–30 seconden duidelijke, natuurlijke spraak per sample;
- verschillende zinnen en liefst verschillende opnamemomenten;
- zo min mogelijk muziek, echo en andere stemmen.

Upload werkt met audioformaten die de actieve browser kan decoderen. Microfoonopname is een progressive enhancement: `getUserMedia` vereist HTTPS, browsertoestemming en ondersteuning door de Home Assistant Ingress-iframe. Als dat niet beschikbaar is, blijft upload volledig werken.

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

Audio in de JSON-contracten is base64-gecodeerde, little-endian signed 16-bit mono PCM met een expliciete sample-rate. De webinterface converteert uploads en microfoonopnames automatisch naar 16 kHz.

## Privacy, backups en herstel

Stem-embeddings zijn biometrische gegevens. Ze blijven lokaal in `/data/speakers`; ruwe audio wordt na verwerking niet opgeslagen. Home Assistant neemt `/data` mee in een App-backup. Verwijderen in de GUI verwijdert het embeddingbestand en de metadata definitief.

## Bekende beperkingen

- Alleen `amd64` is ondersteund.
- Browsermicrofoon kan per Home Assistant-/browserrelease verschillen; upload is de gegarandeerde route.
- Een volledig eerlijke herkenningstest gebruikt andere audio dan de enrollment-samples.
- De upstream custom integration is niet opgenomen: die heeft nog contract-, discovery- en audiocontainerproblemen. Deze release levert de App, GUI en REST-API.
