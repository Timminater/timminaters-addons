# Opslag beheren en analysegegevens exporteren

Klik bovenaan op **Opslag** of open **Data & opslag → Opslag en data-export**. De indicator wordt bij het laden en vervolgens ongeveer iedere minuut bijgewerkt; met Vernieuwen kun je zelf opnieuw meten.

De app telt bestandsgroottes in de datamap: SQLite-database en WAL/journal, Timestral-modelmappen, Epex-cache en overige bestanden. Symbolische links worden niet gevolgd. Dit is de logische bestandsgrootte, niet een filesystemquotum of de exacte hoeveelheid toegewezen schijfblokken. De add-on-image en Home Assistant-backups buiten de datamap vallen erbuiten. De vrije ruimte geldt voor het hele onderliggende bestandssysteem.

## Opruimen

Elke actie vraagt in de app om bevestiging. Maak eerst een export of Home Assistant-backup van gegevens die je wilt bewaren. De knoppen voeren geen periodiek bewaarbeleid in: dit zijn handmatige acties.

- **Oudere historie en prognoses verwijderen**: kies hoeveel dagen je wilt bewaren (7–3650, standaard 90). De app toont vooraf aantallen. Kwartierprijzen, prijsrevisies en uurprijzen worden op interval-einde gefilterd; prognoseruns en bronsnapshots op uitgiftetijd. Alleen records strikt ouder dan de UTC-grens worden verwijderd. Prognosepunten verdwijnen met hun run. Instellingen en recentere gegevens blijven behouden. Het prijsarchief werd voorheen niet automatisch gesnoeid; deze knop kan dat nu expliciet doen. Minder historie kan training, kalibratie en vergelijking beperken.
- **Epex-cache legen**: verwijdert alleen `/data/models/epex`. Epex moet bij een volgende run opnieuw gegevens ophalen en trainen. De ingebouwde modelcode blijft beschikbaar.
- **Ongebruikte Timestral-modellen verwijderen**: verwijdert de bekende modelmappen van varianten die niet als `timestral_model` zijn opgeslagen. Het gekozen model blijft behouden, ook als Timestral uit staat. Opnieuw gebruiken van een verwijderde variant vereist een handmatig bevestigde download.

Tijdens een berekening, historische test of modeldownload wordt opruimen geweigerd. Databasewrites zijn vergrendeld tijdens verwijderen en verkleinen; nieuwe historische tests en downloads kunnen niet tegelijk starten. SQL-verwijderingen worden eerst gecommit; daarna proberen checkpoint en VACUUM de vrije databasepagina's terug te geven aan het bestandssysteem. Als verkleinen niet lukt, meldt de app expliciet dat de records wel verwijderd zijn maar de database nog niet verkleind is. Een bron kan recent verwijderde prijzen bij een volgende synchronisatie opnieuw ophalen; de knop verandert de broninstellingen niet.

## Bestaande database compact kopiëren

Bij het opstarten controleert de add-on of de database al het compacte formaat gebruikt. Bij een oudere database kan het opstarten enkele minuten duren. De add-on controleert de compacte kopie en vervangt daarna het oorspronkelijke databasebestand in één stap. Na succes blijft alleen het compacte actieve bestand over. Bij onvoldoende vrije schijfruimte of een mislukte controle start de add-on met het oorspronkelijke bestand en verschijnt de reden in het logboek.

Voer vanuit de map `stroomvoorspeller` het volgende uit op een consistente databasekopie:

```shell
python -m app.compact_database /pad/naar/stroomvoorspeller.sqlite3 /pad/naar/nieuwe-map
```

SQLite neemt gecommitteerde WAL-wijzigingen mee als het bijbehorende WAL-bestand naast de bron staat. De opdracht leest uit één momentopname en bouwt rechtstreeks een compact bestand in de nieuwe map. Ze controleert de omgezette invoer, de voorspelpunten en de SQLite-integriteit. Reken op vrije ruimte voor het compacte resultaat en tijdelijke SQLite-bestanden. Stop de add-on vóór een omzetting die je als nieuwe productiedatabase wilt gebruiken, zodat er na de momentopname geen nieuwe runs achterblijven. Bewaar het oorspronkelijke bestand als herstelkopie.

## Analyse-export

Kies een periode van 1–3650 dagen of **Alle bewaarde gegevens**. Klik daarna op **Download gegevens (ZIP)**. De app schrijft de bestanden regel voor regel naar een tijdelijke ZIP op schijf. Je browser downloadt het bestand rechtstreeks. Grote exports kosten extra tijd en tijdelijk schijfruimte.

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

Vanaf 0.2.11 bevat een run ook het opgeslagen weergebied en de Timestral-weerkeuze waar beschikbaar. Regionale uurwaarden blijven bruikbaar voor analyse; exacte locatiecoördinaten blijven uitgesloten. De gedeelde Open-Meteo-payloadcache telt mee onder Overig, wordt automatisch begrensd en het blijvende aanvraagbudget wordt niet gewist door Epex-cache opruimen.

Historische runs met opgehaald GFS-archiefweer bevatten vanaf 0.2.12 een reconstructievlag en een eigen modelversie. De export bewaart de initialisatietijd van de oude weersmodelrun en de gesimuleerde uitgifte, zonder locatiecoördinaten. De cache van historische context en Single Runs deelt de blijvende Open-Meteo-limieten met de overige weerbronnen.


Vanaf 0.2.14 staat de SQLite-database met instellingen en prognosehistorie in
`/share/stroomvoorspeller/stroomvoorspeller.sqlite3`, buiten de privé-appdata.
De app verplaatst bij de eerste start de database uit `/media/stroomvoorspeller`
(versie 0.2.13) of anders uit `/data`. De app maakt een gecontroleerde SQLite-kopie inclusief WAL
en verwijdert de oude kopie pas na een succesvolle migratie. Bestaat de externe
database al, dan heeft deze voorrang en blijft een eventuele oude kopie staan.
Als de sharemap niet gekoppeld is, stopt de app met een foutmelding. De mediamap
blijft gekoppeld om bestaande installaties veilig te kunnen migreren.

De oude database en bijbehorende bestanden zijn uitgesloten van appback-ups.
Een Home Assistant-backup kan de sharemap apart meenemen als die in de
back-upselectie staat; controleer je eigen back-upinstellingen. De externe
database bevat ook de appinstellingen. Als de sharemap bij een herstel ontbreekt,
zijn die instellingen en de historie niet vanuit alleen de appback-up te herstellen.
Maak indien gewenst apart een consistente kopie van de database terwijl de app
gestopt is. Een analyse-export is geen volledige herstelbackup.


## Onderzoeksgegevens van het Nederlandse elektriciteitsnet

De lokale verzamelaar vraagt elke zes uur de Nederlandse day-aheadverwachtingen voor verbruik, zon, wind op land en wind op zee op bij Energy-Charts. Iedere reeks wordt afzonderlijk bewaard met de doelkwartieren, eenheid MW, bron, licentie en het tijdstip waarop het antwoord lokaal beschikbaar kwam. Een nieuwe versie overschrijft de vorige niet. Gebruik voor een reconstructie alleen antwoorden die uiterlijk bij de modeluitgifte ontvangen waren. De generatietijd van het API-antwoord bewijst op zichzelf geen eerdere publicatie. De standaardretentie van invoersnapshots is 180 dagen. De analyse-export bevat deze reeksen in `research_forecasts.jsonl`. Bron en naamsvermelding: Fraunhofer Energy-Charts, CC BY 4.0. [API](https://api.energy-charts.info/)

Met een ingestelde NED-sleutel bewaart de verzamelaar ook de vier afzonderlijke NED-reeksen, inclusief revisietijd per uur en de scheiding tussen actuele waarden en verwachtingen. Verzamelen staat los van de optionele NED-proef. Deze gegevens worden niet aan de actieve modellen toegevoegd. De keuze volgt de Nederlandse vraag-, wind- en zonreeksen van [Day Ahead Predictor](https://github.com/corneel27/day-ahead-prediction) en de vastlegging per uitgiftetijd bij [Foretec EnergyBench](https://github.com/ujohn33/foretec-energy-bench).

Nieuwe voorspellingen delen identieke weerblokken. De offline compacte kopie zet bestaande weerblokken om en herbouwt de tabel met voorspelpunten zonder dubbele primaire index. De conversie controleert elke veldwaarde van de punten en de teruggelezen weergegevens, gevolgd door SQLite-integriteits- en sleutelcontroles.
