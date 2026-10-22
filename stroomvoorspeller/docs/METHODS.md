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

De oorspronkelijke adaptieve berekening combineert v4 (25%) met een historisch kwartierpatroon (75%). V2 behoudt dit voor 0–24 uur en vanaf 48 uur; voor 24–48 uur geldt de hieronder beschreven gevalideerde mix. Een
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
en het ingestelde weergebied (vanaf 0.2.11 standaard vier Nederlandse punten). Dit is het **NL single-stage-model**, niet de
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
stilzwijgende download. De adapter gebruikt prijshistorie en standaard expliciete weekendinformatie. Vanaf 0.2.11 zijn regionale weerscovariaten optioneel, standaard uit; zie hieronder.
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
achtergrondtaak: maximaal zeven uitgiftedagen. Kies 48 uur (192 UTC-kwartieren,
minimaal zestien dagen prijsarchief) of 7 dagen (672 UTC-kwartieren, minimaal
21 dagen prijsarchief) per uitgifte. De UI kiest standaard 7 dagen; de API houdt
zonder parameter 48 uur voor bestaande clients. Uitgiftes liggen minstens de
gekozen horizon terug, zodat alle doelkwartieren verstreken zijn. Bij slechts
het minimale archief zijn minder dan zeven uitgiftes beschikbaar. Alleen ingeschakelde
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


## Lokaal adaptief v2: gevalideerde mix voor 24–48 uur

De oude lokale methode koos een historische analogie op basis van diens validatiefout, maar mengde de uitkomst altijd met 25% v4 en 75% analogie. V2 beoordeelt voor doelen die 24 tot minder dan 48 uur na de werkelijke uitgiftetijd liggen de uiteindelijke mix. Kandidaten zijn v4 alleen en analogieën van 7, 14 of 28 dagen (14/28 ook met niveaucorrectie), met 25%, 50%, 75% of 100% analogiegewicht. De historische dagtypen en kwartierposities blijven behouden.

De keuze gebruikt maximaal zeven eerdere dagelijkse uitgiftemomenten om 00:00 UTC, met uurdoelen op horizon 24–48 uur. Elke interne v4-run ziet alleen prijzen die bij dat uitgiftemoment beschikbaar waren en waarvan het kwartier toen al geëindigd was. Doelprijzen moeten bovendien bij de huidige uitgifte geëindigd en beschikbaar zijn. Alle kandidaten gebruiken dezelfde doelset, met minimaal 48 uurdoelen. De huidige weersverwachting wordt nooit achteraf als historische weersverwachting gebruikt. De oorspronkelijke v4-informatie met het actuele weer wordt wel gebruikt in de gekozen mix bij de uiteindelijke prognose.

Minimaal 14 dagen historie is nodig. Bij onvoldoende validatie of een ontbrekende gekozen analogie blijft de bestaande berekening gelden. Voor horizon 0–24 uur en vanaf 48 uur blijft de v1-berekening ongewijzigd. Grenzen van bestaande voorlopige banden worden met de puntcorrectie verschoven; ze worden niet smaller gemaakt en krijgen geen nieuwe dekkingsclaim. Caches voor niveaus en analogieën voorkomen herhaalde scans. Geen NumPy, PyTorch, modeldownload of externe rekenservice nodig.

Modelidentiteit `adaptive-quarter-2` start een eigen score- en kalibratiereeks. Oude `adaptive-quarter-1`-runs blijven in het archief en in exports. De UI toont de nieuwste modelversie per methode; voor een historische vergelijking moet de test opnieuw worden uitgevoerd. Sinds 0.2.10 kan de historische test 192 of 672 kwartieren per uitgifte leveren, met aparte zichtbaarheid van elke dag tot zeven dagen vooruit. Aantallen kunnen verschillen; beoordeel dezelfde horizons en doelkwartieren.

### Onderbouwing en beperking

De wijziging is getoetst met een door de gebruiker aangeleverde, geanonimiseerde export. Er worden geen individuele prijsrecords of bestanden uit die export meegeleverd. Op 13 volledige dagelijkse uitgiftes (7–19 oktober 2026, 1.248 doelen per methode) daalde de historische MAE voor 24–48 uur van 5,6723 naar 5,0828 cent/kWh, circa 10,4%. Op de drie apart gehouden volledige uitgiftes van 17–19 oktober (288 doelen) was dat 3,9508 naar 3,8488 cent/kWh, circa 2,6%. De productie-implementatie reproduceerde die uitkomsten exact; de eerste 24 uur bleven identiek.

Dit is beperkt bewijs uit één installatie en één korte periode. De laatste dagen waren eerder in verkennende analyses bekeken en zijn geen volledig onafhankelijke nieuwe dataset. Het resultaat is geen garantie voor live verbetering, andere leveranciers, andere seizoenen of langere horizons. Modelselectie vond binnen elke uitgifte uitsluitend op eerdere gegevens plaats; toekomstige live prestaties moeten de winst bevestigen. Eenvoudige vaste foutcorrecties en een onderzocht regressiemodel leverden geen consistente verbetering en zijn niet ingebouwd.


## Weekendinformatie en visuele modelvergelijking

Timestral kreeg tot versie 0.2.6 alleen prijswaarden op een regelmatige UTC-grid, zonder expliciete datums of kalendercodes. Het kan een weekpatroon herkennen, maar kent zo geen expliciete zaterdag/zondag. Vanaf 0.2.7 geeft de app standaard één categorische bekende covariaat mee: weekend versus werkdag volgens `Europe/Amsterdam`, voor alle context- én doelkwartieren. De grid blijft UTC met stappen van 15 minuten; Nederlandse daggrenzen en zomer-/wintertijd worden bij het bepalen van de code toegepast. De modelbibliotheek encodeert categorieën uitsluitend tegen de beschikbare contextprijzen, zonder toekomstige doelprijzen. Een weekendprijs hoeft niet lager te zijn; er wordt geen vaste korting of plafond toegevoegd. Feestdagen zijn geen aparte covariaat.

Deze optie is uit te zetten met **Geef Timestral expliciete weekendinformatie** (`timestral_calendar: false`). Alleen kalenderinformatie verandert: bestaande gewichten en de gepinde revisie blijven behouden, zonder nieuwe download. Kalender aan/uit heeft een aparte modelidentiteit en dus eigen scores en kalibratie. Oude resultaten blijven in het archief en komen weer beschikbaar als je de oude configuratie kiest. Weinig context kan betekenen dat er nauwelijks eerdere weekenden aanwezig zijn; gebruik bij voorkeur minimaal 14 dagen, standaard 28. De kalendercode bepaalt niet zelf een leverancierstarief of belastingopslag.

### Beperkte controle op de aangeleverde export

Met 28M zijn zeven dagelijkse reconstructies van 13–19 oktober 2026 op alleen eerdere prijzen uitgevoerd (192 doelen per uitgifte). Alleen het weekend-signaal verlaagde de gemiddelde fout van 3,160 naar 3,030 cent/kWh op 0–24 uur, en van 3,478 naar 3,293 cent/kWh op 24–48 uur. De uitkomsten wisselden per dag; dit is één installatie, één kleine verkennende periode, geen onafhankelijke live toets en geen garantie voor andere modelgroottes. Een uitgebreidere proef met weekdag én kwartierpositie deed het slechter en is niet ingebouwd. Alle vier modelgroottes zijn wel technisch met de weekendcovariaat offline uitgevoerd, inclusief negatieve-prijsruimte en eindige geordende kwantielen.

### Grafieken in Analyse

Kies **Live voorspellingen** of **Historische reconstructie**, de uitgifteperiode (maximaal 45 dagen), de horizon en de methoden. De checkboxes bepalen alleen wat wordt vergeleken; ze schakelen geen berekeningen in. Opslag- en exportacties staan apart op Data & opslag.

De foutbalken tonen MAE en bias op exact dezelfde uitgifte/doel-paren. Eerst worden gemeenschappelijke uitgiftes gevonden, daarna wordt per Nederlandse dag de gezamenlijke uitgifte het dichtst bij 12:00 gekozen. Zo veroorzaakt een ander update-interval niet ten onrechte een lege vergelijking. Alleen metadata en de gekozen prognosepunten worden gelezen; gecomprimeerde invoerhistorie wordt niet uitgepakt voor de grafiek.

De tarieflijn toont maximaal 192 recente gemeenschappelijke doelkwartieren. Bij meerdere gemeenschappelijke uitgiftes voor een doel wordt de nieuwste gekozen, voor alle methoden dezelfde. Werkelijk betekent hier het later gepubliceerde tarief; een kwartier hoeft nog niet verstreken te zijn als de day-ahead-prijs al definitief bekend is. Tooltips vermelden de oorspronkelijke uitgiftetijd. Gaten worden niet geïnterpoleerd. De andere grafiek toont dagelijkse MAE gegroepeerd op uitgiftedag. Overlappende doelen zijn geen onafhankelijke waarnemingen.

Methoden zonder resultaten worden expliciet gemeld en niet als nul fout weergegeven. Zonder gemeenschappelijke uitgiftes/doelen komt er geen rangorde. Eén methode met resultaten wordt expliciet als één methode getoond. De huidige gekozen Timestral-variant en kalenderconfiguratie bepalen welke scoreversie wordt opgehaald. Losse cijfers onder de uitklapbare tabellen kunnen een andere doelset hebben dan de vergelijkingsgrafiek; gebruik de grafieken voor een onderlinge vergelijking.

API: `GET /api/methods?mode=live&horizon=24-48u&days=14&methods=local,v4`. Horizons zijn `all`, `0-24u`, `24-48u`, `48-72u`, `72-96u`, `96-120u`, `120-144u`, `144-168u` en de samenvatting `48-168u`; `methods=none` geeft een lege selectie. Het bestaande antwoord blijft behouden en bevat aanvullend `view` met gemeenschappelijke scores, dagfouten, grafiekpunten, ontbrekende methoden en selectie-informatie. De tijdlijn bevat nu ook de methode-ID, ingeschakelde status en gekozen Timestral-grootte; de modelversie hoort bij de daadwerkelijk teruggegeven prognose.


## Weerlocaties per methode

Vanaf 0.2.11 bepaalt `weather_region` het gebied: `local` (één bevestigde
coördinaat), `nl` (standaard, vier punten) of `europe` (tien punten). De NL-grid
is geïnspireerd op EpexPredictor: (52.69, 6.11), (52.36, 4.90), (51.45, 5.47)
en (53.36, 4.98). Het oude zuidelijke punt (50.51, 5.41) lag in België en is
voor deze grid vervangen door een punt in Noord-Brabant. De uitgebreide grid
voegt Duitsland west/noord, België, Noord-Frankrijk, Denemarken en het Verenigd
Koninkrijk toe. De exacte punten staan in `app/weather_regions.py`.

Adaptief en v4 gebruiken een ongewogen gemiddelde per uur van zon, wind en
temperatuur over alle punten. Dit is een benadering; er zijn geen verzonnen
productiegewichten of getrainde geografische wegingen. Het bestaande v4-model
blijft de onderliggende prijsberekening. De normalisatie met De Bilt is niet
opnieuw voor heel Europa gevalideerd. HA-weerreeksen blijven een apart alternatief;
de app kan hun geografische herkomst niet controleren.

Epex gebruikt per punt afzonderlijke kenmerken voor wind op 80 meter,
temperatuur, straling, luchtdruk en luchtvochtigheid, zoals de upstream
WeatherStore. Elk gebied krijgt een eigen featurecache/modelmap en versie.
De gekozen HA-weerbron verandert de Epex-invoer niet. Buitenlands weer toevoegen
is nog steeds het lokale NL single-stage-model: buitenlandse prijsmodellen en
de internationale tweede Epex-stap zijn hiermee niet ingebouwd.

### Timestral met weer

Timestral kan zonder weer voorspellen doordat het een voorgetraind tijdreeksmodel
is. Eerdere prijzen bevatten dagritmes, weekritmes en eerdere invloeden van vraag,
weer en markt. Dat levert geen kennis van een nog niet in prijzen zichtbare
weersomslag. De optie `timestral_weather` voegt daarom drie numerieke bekende
covariaten toe: gebiedsgemiddelde zon, wind en temperatuur. Standaard uit voor
een bewuste vergelijking en minder rekenwerk. Meer punten/variabelen garanderen
geen lagere fout; er is nog geen lokale nauwkeurigheidswinst aangetoond.

De gedeelde Open-Meteo-batch bevat 35 dagen historie en acht voorspellingsdagen.
Uurwaarden worden op de UTC-kwartiergrid herhaald, niet als afzonderlijke
kwartiermetingen gepresenteerd. Oude ontbrekende context blijft gemaskeerd;
minimaal 192 echte weerkwartieren per kenmerk en een volledige toekomstige
weerreeks zijn nodig. Bij 90 dagen prijscontext ontbreekt dus weer voor het
oudste gedeelte. Er worden geen toekomstige doelprijzen als covariaat gebruikt.
De gewichten blijven dezelfde; alle vier Timestral-groottes zijn offline met
numeriek weer plus weekendinformatie technisch uitgevoerd. Dit is geen
nauwkeurigheidstest. Er is geen nieuwe download nodig.

De optie vereist Open-Meteo; de HA-invoer levert geen historische weercontext.
Een ontbrekende, verkeerde of meer dan zes uur oude weersnapshot leidt tot een
zichtbare methodefout, geen stilzwijgende prijsvariant met hetzelfde scorelabel.
Weer aan/uit en gebied krijgen eigen Timestral-scoreversies. Nieuwe regionale
adaptief/v4-runs hebben een versieachtervoegsel en Epex heeft een nieuwe
feature-identiteit. Oude prognoses blijven opgeslagen.

Historische tests zoeken eerst passende snapshots die vóór de gesimuleerde
uitgifte waren opgeslagen, van het juiste gebied en hooguit zes uur oud.
Vanaf 0.2.12 kun je ontbrekende snapshots laten ophalen via het weerarchief,
voor 1, 4 of 10 punten. De UI kiest standaard **Ontbrekend historisch weer
ophalen (reconstructie)**; **Alleen destijds opgeslagen verwachtingen** blijft
beschikbaar. De API kiest zonder parameter nog steeds alleen opgeslagen weer.

Het archief gebruikt de [Open-Meteo Single Runs API](https://open-meteo.com/en/docs/single-runs-api)
met `models=gfs_global`: een specifieke modelrun die minimaal twaalf uur vóór
de gesimuleerde uitgifte geïnitialiseerd is. Daardoor wordt toekomstig weer
voor de test niet vervangen door achteraf gemeten weer. Timestral krijgt
historische context uit het GFS Historical Forecast Archive; alleen uren vóór
de gekozen modelrun worden daarvan gebruikt. Vanaf die run komen alle waarden
uit diens verwachting. Maximaal 35 dagen historische weercontext wordt vooraf
in één gebiedsaanvraag opgehaald en tussen uitgiftes hergebruikt.

Dit blijft een reconstructie: het GFS-model kan verschillen van het live
Best Match-model en de oorspronkelijke beschikbaarheid op Open-Meteo is niet
volledig bewezen door alleen de initialisatietijd. De conservatieve twaalfuurmarge
is een benadering. De Single Runs-bron archiveert GFS vanaf april 2026; bij
ontbrekende runs, locaties of variabelen worden geen weerwaarden verzonnen.
Archiefweer wordt niet als live weersnapshot opgeslagen en mag uitsluitend bij
de bijbehorende historische uitgifte naar Timestral. Het achtervoegsel
`:weather-archive-gfs-1` houdt de scoreversie apart en de UI vermeldt **archiefweer**.
De oorspronkelijke opgeslagen variant en archiefvariant worden niet binnen één
scoreversie gemengd; deze historische scores tellen niet mee voor live kalibratie.

Bij alleen opgeslagen weer worden ontbrekende Timestral-uitgiftes expliciet
overgeslagen, zonder te adviseren een al correct ingestelde bron te veranderen.
Bij archieffouten tonen de taakstatus en overgeslagen uitgiftes de bronfout.
Adaptief/v4 zonder bruikbare oorspronkelijke of aangevraagde archiefinvoer houden
de bestaande berekening zonder weer. Alle API-aanvragen gebruiken dezelfde cache
en limieten. De achtergrondtaak wacht kort bij een volle minuutlimiet; een
opgelegde afkoelperiode of daglimiet wordt niet omzeild.

Epex houdt zijn al bestaande gereconstrueerde historische weerbron; die scores
zijn geen bewijs met oorspronkelijke datavintages.

### Aanvraaglimieten en cache

Locaties worden gebundeld. Alle Open-Meteo-aanvragen vanuit de hoofdapp en
Epex-processen lopen door `weather_cache.py`, met een bestandslock en blijvende
budgetstatus onder `/data/weather-cache`. Er is geen force-bypass:

- Actuele datasets per gebied/variabelenset minimaal drie uur cache. Datums
  schuiven in een actuele query zonder een extra aanvraag binnen die termijn.
- Historische ranges hebben dertig dagen cache en blijven op bereik gescheiden. Epex groepeert historische data per kalendermaand, zodat opeenvolgende tests dezelfde aanvragen hergebruiken.
- Maximaal twaalf aanvragen per minuut en 48 per UTC-dag, plus een conservatief
  gewogen budget van 512 locatie/periode-eenheden per dag. Dit is een eigen
  limiet, geen exacte weergave van Open-Meteo's quotaberekening.
- Elke poging wordt vóór de netwerkactie geregistreerd, inclusief mislukkingen.
  Normale fouten hebben één uur afkoeling; 429/503 pauzeren alle nieuwe
  aanvragen minstens zes uur of langer volgens `Retry-After`.
- Geldige cache kan tijdens afkoeling worden gelezen. De hoofdapp accepteert
  bij uitval alleen de juiste bron/het juiste gebied tot zes uur oud; Timestral
  verlangt bovendien volledige doeldekking. Gaten worden niet verzonnen.
- Herstarten, handmatig rekenen en het legen van de Epex-featurecache wissen
  het gedeelde aanvraagbudget niet. De payloadcache wordt tot ongeveer 64 MiB
  begrensd; deze opslag telt mee onder Overig. Geen automatisch ophalen als
  uitsluitend Timestral zonder weer aan staat of alle methoden uit staan.

De hoofdapp en Epex delen de bescherming; hun uur-/kwartierdatasets zijn aparte
cache-items. Er wordt dus niet beweerd dat ze altijd één identieke bronaanvraag
kunnen delen. Bij nieuwe gebieden of de eerste Epex-training kan het initiële
historiebereik meerdere begrensde aanvragen vereisen. Providers kunnen alsnog
een eigen blokkade of lagere limiet toepassen; de app probeert dan niet door.

`POST /api/history-test?horizon_hours=168&weather=archive` start de zevendaagse reconstructie met archiefaanvulling. `weather=stored` (API-standaard) gebruikt alleen destijds opgeslagen verwachtingen;
`horizon_hours=48` is de lichtere variant en blijft de API-standaard. Alleen
48 en 168 zijn toegestaan. De taakstatus vermeldt `horizon_hours` en `weather_mode`. Oude
48-uursruns behouden ontbrekende scores voor dag 3–7; daarvoor is een nieuwe
test nodig. De methodecijfers blijven eigen doelsets gebruiken; voor een
onderlinge rangorde gebruikt de grafiek exact gedeelde uitgiftes en doelen.

Vanaf 0.2.12 wordt de productiecache voor een nieuw gekozen gebied direct geraadpleegd. De oude extra uurblokkering kan daarvoor geen lege snapshot meer teruggeven. De gedeelde cache beslist zelf of een netwerkverzoek nodig en toegestaan is; terugschakelen naar een eerder gebied hergebruikt de bestaande dataset. Bronfouten worden aan de live Timestral-status doorgegeven en afkoeling vermeldt de reden en resterende minuten.
