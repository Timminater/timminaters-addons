# Opslag beheren en analysegegevens exporteren

Klik bovenaan op **Opslag** of open **Data & opslag → Opslag en data-export**. De indicator wordt bij het laden en vervolgens ongeveer iedere minuut bijgewerkt; met Vernieuwen kun je zelf opnieuw meten.

De app telt bestandsgroottes in de datamap: SQLite-database en WAL/journal, Timestral-modelmappen, Epex-cache en overige bestanden. Symbolische links worden niet gevolgd. Dit is de logische bestandsgrootte, niet een filesystemquotum of de exacte hoeveelheid toegewezen schijfblokken. De add-on-image en Home Assistant-backups buiten de datamap vallen erbuiten. De vrije ruimte geldt voor het hele onderliggende bestandssysteem.

## Opruimen

Elke actie vraagt in de app om bevestiging. Maak eerst een export of Home Assistant-backup van gegevens die je wilt bewaren. De knoppen voeren geen periodiek bewaarbeleid in: dit zijn handmatige acties.

- **Oudere historie en prognoses verwijderen**: kies hoeveel dagen je wilt bewaren (7–3650, standaard 90). De app toont vooraf aantallen. Kwartierprijzen, prijsrevisies en uurprijzen worden op interval-einde gefilterd; prognoseruns en bronsnapshots op uitgiftetijd. Alleen records strikt ouder dan de UTC-grens worden verwijderd. Prognosepunten verdwijnen met hun run. Instellingen en recentere gegevens blijven behouden. Het prijsarchief werd voorheen niet automatisch gesnoeid; deze knop kan dat nu expliciet doen. Minder historie kan training, kalibratie en vergelijking beperken.
- **Epex-cache legen**: verwijdert alleen `/data/models/epex`. Epex moet bij een volgende run opnieuw gegevens ophalen en trainen. De ingebouwde modelcode blijft beschikbaar.
- **Ongebruikte Timestral-modellen verwijderen**: verwijdert de bekende modelmappen van varianten die niet als `timestral_model` zijn opgeslagen. Het gekozen model blijft behouden, ook als Timestral uit staat. Opnieuw gebruiken van een verwijderde variant vereist een handmatig bevestigde download.

Tijdens een berekening, historische test of modeldownload wordt opruimen geweigerd. Databasewrites zijn vergrendeld tijdens verwijderen en verkleinen; nieuwe historische tests en downloads kunnen niet tegelijk starten. SQL-verwijderingen worden eerst gecommit; daarna proberen checkpoint en VACUUM de vrije databasepagina's terug te geven aan het bestandssysteem. Als verkleinen niet lukt, meldt de app expliciet dat de records wel verwijderd zijn maar de database nog niet verkleind is. Een bron kan recent verwijderde prijzen bij een volgende synchronisatie opnieuw ophalen; de knop verandert de broninstellingen niet.

## Analyse-export

Kies een prognoseperiode van 1–3650 dagen (standaard 30) en klik **Download analyse-export (ZIP)**. De app bouwt een consistent SQLite-leessnapshot en schrijft de JSONL-bestanden regel voor regel naar een tijdelijke ZIP; de hele database wordt niet in RAM gekopieerd. De tijdelijke ZIP wordt na downloaden verwijderd. De gecomprimeerde export is begrensd op 32 MiB. Kies bij overschrijding een kortere periode.

De ZIP bevat:

- `quarter_prices.jsonl`, `quarter_price_revisions.jsonl`, `hourly_history.jsonl`: prijsdata vanaf het begin van de prognoseperiode **plus 90 dagen eerdere invoerhistorie**. Observatie- en publicatietijden blijven behouden, zodat gegevens die toen nog niet bekend waren herkenbaar blijven.
- `forecast_runs.jsonl`: uitgiftetijd, modelversie, methode, live/historische status, prijsveld, eenheid, reeks-ID en de relevante numerieke weerinvoer met tijdstippen. Historische runs blijven als historische reconstructie herkenbaar.
- `forecast_points.jsonl`: prognose en eventuele onder-/bovengrens per doelkwartier, gekoppeld via `run_id`.
- `settings.json`: uitsluitend geselecteerde actuele tarief- en modelinstellingen; geen volledige configuratiedump. Dit is geen historie van instellingen.
- `manifest.json`: exportschema, periode, aantallen en beperkingen.
- `README.txt`: koppel- en interpretatie-instructies.

Reeks-ID's vervangen de oorspronkelijke entiteitsnamen met per-export gezouten hashes. Dezelfde reeks heeft binnen één export dezelfde ID; IDs van verschillende exports zijn niet onderling vergelijkbaar. Prognoseruns gebruiken dezelfde geanonimiseerde archiefidentiteit als de prijsdata. Coördinaten, weerentiteiten, tokens, URL's uit foutmeldingen, ruwe bronantwoorden, complete instellingen, per-run gecomprimeerde ruwe invoerhistorie en modelgewichten worden niet opgenomen. Relevante weerinvoer wordt op toegestane numerieke velden gefilterd. Prijs- en weerpatronen kunnen nog persoonlijke informatie bevatten; beoordeel daarom zelf met wie je deelt.

Er wordt niets automatisch verstuurd. Upload de ZIP zelf in de chat om voorspelfouten te laten onderzoeken. De export is bedoeld voor analyse en vormt geen herstelbare Home Assistant-backup. Eerder verwijderde data kan niet worden geëxporteerd; volledige reconstructie van oude modelruns is niet altijd mogelijk.

## API

- `GET /api/storage?days=90`: bestandsgroottes, vrije ruimte, aantallen en opruimpreview.
- `POST /api/storage/cleanup`, `Content-Type: application/json`: `{"action":"history","days":90,"confirmed":true}`. Andere acties: `epex_cache`, `unused_models`. Alleen bekende acties en modelmappen zijn toegestaan; geen willekeurige paden.
- `GET /api/export?days=30`: ZIP als attachment. Bij bezette berekening, ongeldige periode of te grote export volgt een uitlegbare foutmelding.

Gebruik deze endpoints via de bestaande Home Assistant-ingress. Er is geen extra poort of externe opslagdienst nodig.
