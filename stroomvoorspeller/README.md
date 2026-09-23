# Stroomvoorspeller voor Home Assistant

Een lokale Home Assistant App met een eigen Ingress-pagina voor bekende en voorspelde kwartierprijzen. De App leest alleen de Home Assistant Core API, kan weersverwachtingen van Open-Meteo ophalen en bewaart keuzes, prijsarchief, invoersnapshots en modeluitkomsten op de installatie zelf onder `/data`. Er is geen publieke feed en geen apparaatbediening.

## Bron en status

De rekenregels zijn geïnspireerd door [`Mr-MIle/stroomvoorspeller` op commit `175a0ed975c409fbf9329a62687a7952ee745514`](https://github.com/Mr-MIle/stroomvoorspeller/commit/175a0ed975c409fbf9329a62687a7952ee745514). De gebruikte v4-berekeningen zijn voor deze App opnieuw geïmplementeerd en met vaste referentiegevallen vergeleken. Kwartierprijzen uit een gekozen tariefsensor zijn een andere doelgrootheid dan kale EPEX-prijzen in EUR/MWh. De methode en beperkingen staan in [MODEL.md](docs/MODEL.md).

Een prognose blijft **voorlopig** tot er minstens 35 dagen met 95% kwartierdekking én zeven volwassen dagelijkse voorspellingsruns met bruikbare evaluatie zijn. Een dagelijkse run vereist ten minste 92 van de eerste 96 voorspelde kwartieren met werkelijke prijs en een causale basislijn. Daarna heet de reeks **lokaal geëvalueerd** en toont de pagina de gemeten fout; dit is geen nauwkeurigheidsgarantie. De getoonde modelmarge is indicatief en niet als kwartierband gekalibreerd.

## Installatie en lokale bouw

Voeg `https://github.com/Timminater/timminaters-addons` toe als repository in de Home Assistant App-winkel. Vernieuw de winkel, kies **Stroomvoorspeller** en installeer of werk de App bij. De repository verwijst naar de versiegebonden multi-architectuurimage `ghcr.io/timminater/addon-stroomvoorspeller:0.1.1`. Controleer vóór installatie dat de GitHub Actions-build voor deze versie is geslaagd en de image publiek beschikbaar is. Dit document voert geen installatie in een live Home Assistant uit.

De Dockerfile is ook los te bouwen:

```sh
docker build -t stroomvoorspeller-ha-app:local .
```

In productie verzorgt Supervisor de toegang via Ingress op containerpoort 8099. Er is geen hostpoort ingesteld. Alleen beheerders zien de zijbalkpagina. `homeassistant_api: true` geeft de App toegang tot de officiële Supervisor-proxy. De backend gebruikt daarvan uitsluitend GET-verzoeken.

## Gebruik

1. Open na installatie de pagina **Stroomvoorspeller** in de Home Assistant-zijbalk.
2. Kies een tariefsensor. `sensor.zonneplan_current_quarter_hourly_electricity_tariff` is alleen een voorstel; de App controleert of de sensor op deze installatie bestaat.
3. Controleer prijsveld en eenheid. Als de sensor geen eenheid meelevert, kies die expliciet. De App leidt de eenheid nooit uit de getalsgrootte af.
4. Kies Open-Meteo of HA-weerentiteiten. De Home-locatie van deze HA-installatie wordt als voorstel ingevuld. Controleer die en sla Instellingen op voordat zij voor Open-Meteo wordt gebruikt; de App bewaart de bevestigde locatie lokaal.
5. Bekijk de horizontaal scrollbare tijdlijn met alle bekende en voorspelde kwartieren, de kwartiertabel, goedkoopste aaneengesloten vensters en het kwaliteitsblok. De grafiek begint bij nul, behalve wanneer negatieve prijzen of modelmarges voorkomen. Wijs een balk aan, gebruik het toetsenbord of tik erop om prijs, bron en eventuele indicatieve marge te zien.
6. Gebruik **Bereken nu** voor een directe nieuwe modelrun. Onder **Instellingen** kies je het automatische controle- en berekeninterval: 5, 15, 30, 60 of 120 minuten. Bij een automatische cyclus rekent het model alleen opnieuw wanneer de invoer is veranderd. De Open-Meteo-bron blijft maximaal één keer per uur bevraagd.

Bij uitval toont de App bewaarde prijzen met een verouderingsmelding. Een entiteitswissel wist het oude archief niet; de reeksen blijven per entiteit gescheiden. Alleen werkelijk bekende kwartieren komen in het kwartierarchief. Uurgemiddelden worden nooit opgesplitst in vier zogenaamde waarnemingen.

## Ontwikkelcontrole

Gebruik een repository-lokale basetemp:

```sh
pytest --basetemp .pytest-runtime tests
```

De point-in-time evaluatie in `app/backtest.py` rekent met bewaarde invoersnapshots. Zij mag geen achteraf gemeten weer als modelinvoer gebruiken. Zonder voldoende volwassen lokale data rapporteert de App geen bewezen nauwkeurigheid.

Na opbouw van het archief kunnen de fouten per horizon lokaal worden bekeken:

```sh
python -m app.backtest --data-dir /data --entity-id sensor.zonneplan_current_quarter_hourly_electricity_tariff --price-field tax_included --tariff-unit EUR/kWh
```

De uitvoer bevat MAE en bias over alle gerealiseerde voorspellingen (`n`). Vergelijk `paired_model_mae` met `baseline_mae`: beide gebruiken precies de `paired_n` kwartieren waarvoor ook een causale basislijn bestaat. Waar berekenbaar vergelijken `window_regret` en `baseline_window_regret` de vensterkeuzes op dezelfde gerealiseerde kwartieren. `band_coverage` blijft leeg zolang er geen lokaal gekalibreerde kwartierband is.

## Herkomst en privacy

Zie [architectuur](docs/ARCHITECTUUR.md), [model en afwijkingen](docs/MODEL.md) en de [officiële communicatie](https://developers.home-assistant.io/docs/apps/communication/) en [App-configuratie](https://developers.home-assistant.io/docs/apps/configuration/) van Home Assistant. Deze App gebruikt geen GitHub Actions, Vercel, publieke `ha.json` of opslag van persoonsgegevens buiten de installatie. Bij keuze voor Open-Meteo verlaten alleen de gekozen coördinaten en de benodigde weerquery de installatie.
