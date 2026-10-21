# Lokale rekenmethoden

Alle methoden rekenen **in dezelfde add-on**. Er zijn geen instellingen voor een
EpexPredictor- of Timestral-server en er wordt geen prijshistorie naar een externe
rekenservice verstuurd. Oude service-URL's uit 0.2.0 worden genegeerd en niet meer
via instellingen aangeboden.

Onder Instellingen kan elke methode afzonderlijk aan of uit. Standaard is alleen
**Lokaal adaptief** aan. De actieve methode bepaalt grafiek en MQTT; andere
ingeschakelde methoden bewaren hun eigen resultaten voor Analyse. Alle methoden
mogen uit: bekende prijzen blijven zichtbaar, prognoses verdwijnen.

## Resources

De CPU-bibliotheken voor alle methoden zitten in de image. Daardoor is de image
groter dan die van 0.2.0; de Timestral-gewichten zitten er niet in. De lokale
amd64-testimage was circa 1,2 GiB uitgepakt, exclusief modelgewichten. Uitgeschakelde
methoden importeren geen zware ML-bibliotheken en starten geen training,
inferentie of download. De webserver blijft onafhankelijk van die bibliotheken.

Epex en Timestral starten elk een tijdelijk **lokaal Python-proces**. Er draait
maximaal één ML-proces of modeldownload tegelijk. Na afloop wordt het proces
beëindigd en komt zijn modelgeheugen vrij. Uitschakelen stopt ook een lopende
ML-berekening; de eerste controle gebeurt binnen circa één seconde. De instelling
**CPU-threads voor lokale ML** begrenst beide modellen tot 1–4 threads (standaard 2).

Automatische Epex-runs zijn minimaal een uur uit elkaar; Timestral-runs minimaal
vijftien minuten. Een langer algemeen berekeninterval blijft van kracht.
**Bereken nu** kan bewust een nieuwe run starten. Bij uitval wordt niet heimelijk
van methode gewisseld; de foutstatus en uitgiftetijd blijven zichtbaar.

## Lokaal adaptief en oorspronkelijke v4

Lokaal adaptief combineert v4 (25%) met een historisch kwartierpatroon (75%). Een
validatievenster van zeven dagen kiest uit 7-, 14- en 28-daagse patronen, met of
zonder niveaucorrectie op de laatste twee dagen. De validatie-invoer moet vóór de
gesimuleerde uitgiftetijd beschikbaar zijn. Bij te weinig validatiepunten valt
deze methode terug op v4, met een melding. De mix is een ontwerpkeuze, geen
bewezen universeel optimale verhouding. De oorspronkelijke v4 blijft als aparte
referentiemethode beschikbaar.

## Ingebouwde EpexPredictor NL

De add-on bevat de oorspronkelijke LightGBM-modelmodules van
[b3nn0/EpexPredictor](https://github.com/b3nn0/EpexPredictor) op commit
`676ed37da4968c4a94d3fece39b948d5988ff580`, inclusief BSD-3-Clause-licentie.
Training en voorspelling vinden lokaal plaats, met de Nederlandse configuratie
en vier Nederlandse weerlocaties. Dit is het **NL single-stage-model**, niet de
volledige internationale tweetrapsstapel van de publieke service. Daardoor zijn
de prestatiecijfers van die service niet rechtstreeks van toepassing.

De trainingshistorie is instelbaar van 30 tot 180 dagen (standaard 180). Een
kortere periode bespaart rekentijd en geheugen. Minimaal veertien dagen complete
markt-/weerkwartieren zijn vereist. Invoer en het laatst getrainde model worden
onder `/data/models/epex` bewaard; elke run traint met de beschikbare begrensde
invoer. De data-cache voorkomt het telkens opnieuw downloaden van alle historie. Een
volledige weerprognose-cache jonger dan een uur wordt hergebruikt; bij bronuitval
mag een volledige cache tot zes uur oud dienen, met vermelding in de run.

Lokaal rekenen betekent hier niet volledig offline: actuele en historische
**brondata** komen van Energy-Charts/Fraunhofer ISE en Open-Meteo
(`api.energy-charts.info`, `api.open-meteo.com` en
`historical-forecast-api.open-meteo.com`). Er worden geen voorspellingen bij
`epexpredictor.batzill.com` opgehaald. Het NL-model heeft geen ENTSO-E-token nodig;
upstream ondersteunt een optionele ENTSO-E-bronfallback. Nord Pool wordt door de
app niet ingeschakeld. Bronvoorwaarden en fair use blijven van toepassing.

Lokale aanpassingen aan de upstream-code staan in `app/vendor/README.md`: proxy-
en CA-ondersteuning met verificatie, begrensde timeouts/threads en geen opvulling
van ontbrekende kwartierprijzen. Training gebruikt uitsluitend vóór de uitgifte
geëindigde marktkwartieren, ook als een cache al day-aheadprijzen bevat. Alleen
kwartieren met complete weerkenmerken worden voorspeld; gaten blijven gaten.

Bij Energy-Charts gebruikt de app de bestaande kale/all-in-keuze en gedateerde
tariefcomponenten. Voor een HA-tarief is een expliciet bevestigde omrekening nodig:

```
HA-prijs (EUR/kWh) = kale marktprijs (EUR/kWh) × vermenigvuldiger + vaste opslag
```

Die formule moet bij het gekozen belastingveld en contract passen; de app raadt
geen leveranciersformule. Een gewijzigde omrekening of trainingsperiode begint
een eigen Epex-evaluatiereeks. De onderliggende marktbron gebruikt CC BY 4.0;
bronvermelding blijft EpexPredictor, Energy-Charts/Fraunhofer ISE en Open-Meteo.

## Ingebouwde Timestral-modelkeuze en handmatige download

De officiële Timestral-bibliotheek is ingebouwd op commit
`a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff` (Apache-2.0). Er is geen aparte
machine, container, server-URL of luisterpoort nodig.

1. Open Instellingen en kies **5M, 28M, 123M of 209M** bij **Timestral lokaal installeren**. 28M is de standaard voor nieuwe installaties; bestaande 209M-instellingen en downloads blijven behouden.
2. Lees de modellicentie en vink de downloadbevestiging aan.
3. Klik **Download Timestral-model** en wacht op de lokale gereedmelding.
4. Schakel Timestral in, kies het eventueel als actieve methode en sla de instellingen op. Opslaan activeert de gekozen variant; vooraf downloaden verandert de opgeslagen selectie niet.

Alleen inschakelen start **geen download**. De downloadactie is afzonderlijk en
werkt ook als de methode nog uit staat. Het endpoint
`POST /api/timestral/download` vereist `{"confirmed": true, "model": "28M"}`; zonder model geldt de opgeslagen keuze. Status en voortgang staan op `GET /api/timestral?model=28M` (zonder query: opgeslagen keuze). Alleen deze vier modelnamen worden geaccepteerd. De modeldownload doet een expliciete aanvraag bij
Hugging Face, controleert de gepinde bestanden en bewaart ze onder
`/data/models/timestral-5m`, `timestral-28m`, `timestral-123m` of `timestral-209m`. Na een herstart of update blijven die bestanden
behouden. Een onderbroken download kan handmatig worden hervat. Bij een bezet
ML-proces verschijnt een melding; probeer de download dan opnieuw na die run.

Elke variant heeft een eigen gepinde revisie, SHA-256 voor gewichten en configuratie en een aparte map. De vaste gegevens staan in `app/timestral_models.py`.

| Model | Gewichten (decimale MB, afgerond) | Gebruik |
| --- | ---: | --- |
| 5M | 21 MB | Zuinige optie |
| 28M | 111 MB | Standaard voor nieuwe installaties |
| 123M | 490 MB | Optioneel voor krachtigere systemen |
| 209M | 837 MB | Grootste optie, bestaande installatie behouden |

Downloadgrootte is geen RAM-specificatie. PyTorch blijft in de image aanwezig, ongeacht de gekozen variant. Eerder gedownloade varianten blijven op schijf; slechts één variant wordt gebruikt bij een run. Wisselen start een eigen score- en kalibratiereeks op basis van modelgrootte, revisie en contextlengte; de oorspronkelijke 209M-identiteit blijft exact behouden. De vergelijking toont de momenteel geselecteerde variant en diens scores. Terugschakelen maakt de eerdere scores weer beschikbaar; varianten worden niet gelijktijdig vergeleken. Een lopende inferentie met de vorige variant wordt gestopt.

Naast de gewichten is ruimte nodig voor image, metadata en eventueel onvolledige downloads. SHA-256 van gewichten én configuratie wordt
gecontroleerd; ook vóór inferentie worden de checks herhaald. De modellicentie
wordt mee gedownload. De gewichten hebben een **Modified MIT**-licentie met een
beperking voor bedrijven/werkgevers boven USD 20 miljoen geconsolideerde
maandomzet. Zie de [modellicentie](https://huggingface.co/mistralai/Timestral-209M/blob/main/LICENSE).

Inferentie gebruikt uitsluitend de lokale bestanden, met offline-modus aan.
Ontbrekende of beschadigde bestanden veroorzaken een melding; nooit een
stilzwijgende download. De adapter gebruikt prijshistorie, zonder weerscovariaten.
Gaten blijven ontbrekend en negatieve prijzen worden toegestaan. De q10–q90-band
is modelonzekerheid, geen lokaal bewezen 80%-dekking.

De context is instelbaar van 2 tot 90 dagen (standaard 28). Gebruik bij voorkeur
bij grotere modellen minimaal 4 GB beschikbare RAM; dat is een startbudget, geen garantie voor elk
apparaat of elke context. Minder context/threads bespaart resources. Tijdens
ontwikkeling werkte 28 dagen context met zeven dagen horizon offline binnen een
containerlimiet van 4 GiB RAM en twee CPU's. Deze controle zegt niets over de
nauwkeurigheid op jouw installatie.

## Bestaande historie en vergelijking

De app bewaart echte kwartieren in zijn eigen archief. Bij een nieuwe
HA-configuratie vraagt hij standaard maximaal 90 dagen Recorder-historie op,
instelbaar van 14–180 dagen. Een groter bereik reset de ophaalcursor. Verwijderde
Recorder-data en uurstatistieken kunnen geen echte kwartierdata vervangen. Voor
`tax_excluded` blijft het bestaande voorbehoud over de betekenis van oude HA-
states gelden.

**Test bestaande historie** onder Analyse start handmatig een begrensde
achtergrondtaak: maximaal zeven recente uitgiftedagen, per dag 96 toekomstige
UTC-kwartieren, met minimaal zestien dagen prijsarchief. Alleen ingeschakelde
methoden draaien. Ook Epex kan nu lokaal opnieuw trainen voor elke historische
uitgifte. Timestral vereist vooraf lokaal gedownloade gewichten.

Historische resultaten heten **reconstructie**. Alleen vóór de simulatie geëindigde
prijzen worden modelinvoer. Epex gebruikt gereconstrueerde historische markt- en
weerdata; de oorspronkelijke datavintages en oude weersverwachtingen zijn niet
gegarandeerd. Deze scores blijven gescheiden van destijds opgeslagen live
prognoses en tellen niet mee voor volwassenheid of intervalkalibratie.

Elke run bewaart methode, versie, uitgiftetijd, prijsidentiteit, belastingveld,
eenheid, invoer en evaluatiemodus. De lokale adapters hebben nieuwe versies,
zodat scores uit de oude externe route niet met de nieuwe modellen worden
gemengd. Analyse toont MAE, bias en aantallen; de gepaarde MAE gebruikt dezelfde
uitgiftetijden en doelkwartieren van ingeschakelde methoden met resultaten.
Ontbrekende paren blijven ontbreken en worden niet als nul fout meegeteld.
