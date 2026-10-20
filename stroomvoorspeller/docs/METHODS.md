# Rekenmethoden en vergelijking

Via Instellingen kan elke methode afzonderlijk aan of uit. Standaard is alleen
**Lokaal adaptief** aan; er worden geen Timestral-gewichten opgehaald en geen
EpexPredictor-aanvragen gedaan zolang die methoden uit staan. Alle methoden mogen
uit: bekende prijzen blijven dan beschikbaar, prognoses verdwijnen uit grafiek
en MQTT. De actieve methode moet ingeschakeld zijn wanneer een methode aan staat.
Andere ingeschakelde methoden bouwen hun eigen vergelijkingsreeks op.

Uitschakelen voorkomt volgende berekeningen. Een al lopende aanvraag mag
afronden. Historische tests controleren de schakelaars opnieuw vóór elke run.
Er wordt niet automatisch van methode gewisseld bij uitval; de eigen foutstatus
blijft zichtbaar onder Analyse. Daardoor is duidelijk van welke methode een
prognose afkomstig is. De laatste opgeslagen prognose blijft bij een tijdelijk
mislukte berekening beschikbaar met de bestaande uitgiftetijd.

## Lokale methoden

**Lokaal adaptief** combineert de oorspronkelijke v4-uitkomst (25%) met een
historisch kwartierpatroon (75%). Een validatievenster van de afgelopen zeven
dagen kiest tussen 7-, 14- en 28-daagse patronen, met of zonder een robuuste
prijsniveaucorrectie op de laatste twee dagen. Werkdagen en weekenden en lokale
kwartierposities blijven onderscheiden. Elke validatievoorspelling gebruikt
uitsluitend prijzen van vóór zijn eigen simulatietijd; de doelprijzen komen
niet in die training terecht. Bij onvoldoende validatiepunten blijft v4 actief
binnen deze methode, met een expliciete melding. De mixverhouding is een vaste
ontwerpkeuze, geen bewezen universeel optimale verhouding.

**Oorspronkelijke v4** blijft afzonderlijk beschikbaar voor vergelijking.
De adaptieve methode is experimenteel. Een regressietest toont minder fout in
een synthetische prijsniveausprong; dat bewijst geen verbetering bij elk contract,
elk weersregime of op een bestaande installatie. Beide methoden blijven hun
ongekalibreerde marges als indicatie tonen totdat voldoende eigen live fouten
voor kalibratie beschikbaar zijn.

## EpexPredictor NL

De app gebruikt het `/prices`-endpoint van
[b3nn0/EpexPredictor](https://github.com/b3nn0/EpexPredictor). De standaard-URL is
`https://epexpredictor.batzill.com`; een zelf gehoste compatibele service kan ook.
De aanvraag kiest Nederland, EUR/MWh en UTC. De app verstuurt geen HA-prijshistorie
of Supervisor-token. De publieke dienst is gratis op basis van fair use en geeft
geen beschikbaarheidsgarantie. Automatische aanvragen en foutpogingen worden
begrensd tot maximaal eenmaal per uur; **Bereken nu** kan dit bewust doorbreken.

Het antwoord bevat ook bekende prijzen. Waarden tot en met `knownUntil`, plus
lokaal al bekende kwartieren, worden niet als voorspelling opgeslagen. De lokale
ophaaltijd is de uitgiftetijd voor vergelijking: de upstream modelversie en
werkelijke berekentijd zijn niet beschikbaar. Scores identificeren daarom de
adapterversie, niet een onveranderlijk upstream model.

Voor de Energy-Charts-prijsbron gebruikt de app de bestaande kale/all-in-keuze
en gedateerde tariefcomponenten. Voor een HA-tarief moet de gebruiker expliciet
een omrekening bevestigen:

```
HA-prijs (EUR/kWh) = kale marktprijs (EUR/kWh) × vermenigvuldiger + vaste opslag
```

Dit is nodig om beursprijzen niet met een all-intarief te verwarren. De formule
moet bij de **gekozen belastingvariant en eenheid** passen. Er wordt geen
leveranciersformule geraden. Bij gewijzigde omrekeninstellingen begint een nieuwe
Epex-evaluatiereeks; eerdere prognoses blijven bewaard.

Bronvermelding: EpexPredictor (Adrian Batzill; BSD-3-Clause voor de upstream code),
Energy-Charts/Fraunhofer ISE en Open-Meteo. Deze app integreert de API en bevat geen
gekopieerde EpexPredictor-modelcode. Upstream-code-inspectie:
`676ed37da4968c4a94d3fece39b948d5988ff580`.

## Timestral-209M: optionele rekenservice

De gewone add-on blijft licht en vereist geen PyTorch. De optionele service in
`timestral_service/` draait op een aparte Linux-machine of container met CPU en
voldoende geheugen. Bouw en start die bijvoorbeeld op de Docker-host:

```sh
cd stroomvoorspeller/timestral_service
docker build -t stroomvoorspeller-timestral .
docker run -d --name stroomvoorspeller-timestral --restart unless-stopped \
  --cpus 2 --memory 4g \
  -e TIMESTRAL_THREADS=2 \
  -v stroomvoorspeller-timestral-cache:/cache \
  -p LAN_IP:8098:8098 stroomvoorspeller-timestral
```

Vervang `LAN_IP` door het private adres van de Docker-host en vul in de app
`http://LAN_IP:8098` in. Bij een gedeeld privaat containernetwerk kan de service
via zijn servicenaam worden benaderd zonder hostpoort. Publiceer deze endpoint
niet op internet: hij heeft geen authenticatie en ontvangt de prijshistorie van
het gekozen tarief. Gebruik voor toegang over een onbetrouwbaar netwerk een
beveiligde infrastructuur met TLS/toegangscontrole.

De 4 GB is een startbudget, geen bewezen maximum. Meer context vraagt meer
geheugen. De standaardcontext is 28 dagen, instelbaar van 2 tot 90 dagen. De worker
begrenst CPU-threads tot vier en verwerkt maximaal één aanvraag tegelijk.
Automatische berekeningen en foutpogingen zijn minimaal vijftien minuten uit
elkaar. De eerste aanvraag downloadt het model naar de cache; die eerste download
kan langer duren dan de aanvraaglimiet. Een volgende poging hergebruikt de cache.
Elke inferentie draait in een tijdelijk proces; na afloop geeft dat zijn
modelgeheugen vrij. Dit kost extra laadtijd maar vermijdt permanent model-RAM.

De service gebruikt de officiële `timestral`-bibliotheek op commit
`a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff`, PyTorch 2.6.0 CPU en de gepinde
[Timestral-209M](https://huggingface.co/mistralai/Timestral-209M)-checkpoint
`9874d252ab0aae16f7f7c999f98f98d6ad7d8365`.
Netwerktoegang tot GitHub en de Python/PyTorch-registers is nodig voor de bouw;
Hugging Face en diens downloadhosts zijn nodig bij de eerste modeldownload.
TLS- en artifactverificatie blijven ingeschakeld. Een beheerde build met eigen
CA kan die met `--secret id=proxy_ca,src=/pad/naar/ca-bundle.pem` aanbieden.

Deze eerste adapter gebruikt **prijshistorie**, zonder weerscovariaten. Gaten
blijven ontbrekend; er worden geen gemeten prijzen geïnterpoleerd. Reeds bekende
aaneengesloten day-aheadkwartieren mogen als context dienen. Negatieve prijzen
blijven toegestaan, ook bij een uitsluitend positieve context. De q10–q90-band
is modelonzekerheid en geen lokaal bewezen 80%-dekking. `GET /health` bewijst
bereikbaarheid; alleen een geslaagde `POST /forecast` bewijst inferentie.

De codebibliotheek is Apache-2.0; de modelgewichten vallen onder een **Modified MIT**
licentie, inclusief een beperking voor bedrijven/werkgevers met meer dan
USD 20 miljoen geconsolideerde maandelijkse omzet. Zie de
[modellicentie](https://huggingface.co/mistralai/Timestral-209M/blob/main/LICENSE)
voor de voorwaarden vóór gebruik of distributie.

## Bestaande historie gebruiken

De app bewaart alle echte kwartieren in zijn eigen prijsarchief. Bij een nieuwe
HA-configuratie vraagt hij standaard maximaal 90 dagen Recorder-historie op,
instelbaar van 14 tot 180 dagen. Vergroten van deze instelling reset de
ophaalcursor; de volgende verversing vult het beschikbare bereik aan. HA moet die
gegevens nog hebben: verwijderde Recorder-data en grovere uur-/statistiekwaarden
kunnen niet als echte kwartierprijzen worden hersteld. De bestaande beperking
voor tax_excluded-HA-historie blijft van kracht: de betekenis van oude states
is niet bewezen gelijk aan het gekozen belastingveld.

Onder Analyse start **Test bestaande historie** handmatig een achtergrondtaak.
De taak gebruikt maximaal zeven recente uitgiftedagen, voorspelt per dag de
volgende 96 UTC-kwartieren en vereist minimaal zestien dagen prijsarchief, waarvan
veertien dagen voor de eerste training. Alleen ingeschakelde lokale methoden en
Timestral worden uitgevoerd; Timestral moet dus ook voor historische tests
bereikbaar zijn. Onvoldoende historische dekking levert weinig of geen paren op,
geen opgevulde doelprijzen. De taak is begrensd en herhaalbaar zonder dubbele
resultaten voor dezelfde invoer en modelversie.

Deze resultaten heten **historische reconstructie**. Ze gebruiken het nu
beschikbare prijsarchief en alleen vóór elk simulatietijdstip geëindigde
kwartieren, zonder historische weersverwachtingen. Achteraf gecorrigeerde
prijswaarden en oude publicatievintages zijn daarmee niet te reconstrueren.
Die scores blijven gescheiden van live prognoses en tellen niet mee voor
volwassenheid of intervalkalibratie. Ze bieden wel meteen een eerste vergelijking
wanneer voldoende oude kwartieren beschikbaar zijn.

De publieke EpexPredictor-API biedt geen archief van destijds afgegeven prognoses.
Epex krijgt daarom geen verzonnen historische score. Daar begint de eerlijke
vergelijking zodra de app echte prognoses ophaalt en bewaart.

## Scores en opslag

Elke run bewaart methode, adapter/modelversie, uitgiftetijd, prijsidentiteit,
belastingveld, eenheid, invoer en evaluatiemodus. Oudere bestaande runs blijven
leesbaar. Live scores en gekalibreerde banden worden per methode én versie
berekend. De actieve methode bepaalt grafiek en MQTT; historische runs kunnen
nooit de actieve prognose vervangen.

Analyse toont MAE, bias en aantallen per methode via `/api/methods`, met in de API
ook de horizons 0–24, 24–48 en 48–168 uur. Eén run per lokale dag begrenst de
invloed van frequente berekeningen. De gepaarde MAE gebruikt uitsluitend dezelfde
uitgiftetijden en doelkwartieren van ingeschakelde methoden met resultaten.
Wanneer er geen overlap is verschijnt geen gepaarde score. Ontbrekende methoden
of mislukte aanvragen worden niet als nul fout meegeteld. Historische en live
scores mogen door hun verschillende invoer niet rechtstreeks als één ranglijst
worden gelezen.
