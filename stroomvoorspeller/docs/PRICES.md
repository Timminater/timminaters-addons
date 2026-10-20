# Marktprijs en all-in keuze

De bestaande bron **Home Assistant-tariefentiteit** blijft de standaard. Met **Energy-Charts Nederland** haalt de app rechtstreeks de Nederlandse day-ahead-reeks op via `https://api.energy-charts.info/price?bzn=NL`. De API meldt bedragen in `EUR / MWh`; de app converteert met delen door 1000 naar `EUR/kWh`. De UTC-starttijd identificeert een kwartier van exact 15 minuten. De bronattributie is Energy-Charts.info (Fraunhofer ISE), CC BY 4.0. De app bewaart het ophaaltijdstip als `published_at` en neemt alleen waarden mee die bij de modelrun al waren opgehaald. De bron wordt maximaal eens per vijf minuten opgevraagd, ruim onder de standaardlimiet van twee verzoeken per minuut.

**Kale beursprijs** is de day-ahead marktwaarde zonder btw, energiebelasting of leveranciersvergoeding. **All-in afnameprijs** wordt berekend als:

```text
(marktprijs EUR/kWh + energiebelasting excl. btw) * (1 + btw/100) + inkoopvergoeding incl. btw
```

De instellingen bewaren leverancier, drie componenten en een inclusieve toepassingsperiode. Energiebelasting voor 2026 is €0,09161/kWh excl. btw voor de huishoudelijke schijf tot en met 10.000 kWh; btw is 21%. De lokale leverancierslijst bevat de 25 Nederlandse particuliere leveranciers uit de eerste post van het [Tweakers-topic](https://gathering.tweakers.net/forum/list_messages/2049754), met peildatum **4 augustus 2026** en opslagen inclusief btw. Dit zijn tariefvoorstellen: controleer je contract. Maandkosten, saldering en teruglevering zijn niet inbegrepen.

Dezelfde lijst voedt de EpexPredictor-omrekening naar HA-prijzen en de Energy-Charts all-in-instellingen. De berekende Energy-Charts-reeks wordt gebruikt voor grafiek, modelinvoer, archief, evaluatie en de bestaande all-in MQTT-sensor. Er worden nergens nogmaals leverancierskosten boven op een bestaande HA-prijsreeks gezet. Lokaal adaptief, oorspronkelijke v4 en Timestral voorspellen rechtstreeks op de gekozen reeks.

Nieuwe leveranciersvoorstellen voor Energy-Charts lopen van **4 augustus t/m 31 december 2026**. Dat is een begrensde toepassingsperiode, geen bevestigde contractperiode: de bron bewijst geen geldigheid vóór de peildatum of tot het einde van het jaar. De gebruiker kan de componenten en datums aanpassen of Eigen tarief kiezen. De bestaande Zonneplan Epex-preset met onafhankelijke leveranciersbron behoudt zijn eerder vastgelegde 2026-periode. Bestaande opgeslagen tarieven worden niet overschreven door de nieuwe lijst. All-in wordt voor kwartieren buiten de ingestelde periode niet gemaakt. Voor Tibber blijft de bestaande contractbevestiging nodig; de nieuwe tabel noemt €0,02480/kWh incl. btw.

Prijsbron, prijskeuze en componenten zijn onderdeel van de interne archief- en modelrunidentiteit. Een wijziging begint daarom een eigen reeks; historische prijzen en voorspellingen van een andere configuratie worden niet hergebruikt. Historische all-in kwartieren buiten de ingevoerde geldigheidsperiode worden weggelaten, niet teruggerekend met actuele componenten.

## Instellingen API

`PUT /api/settings` accepteert, naast de bestaande velden:

| Veld | Waarden / eenheid |
| --- | --- |
| `price_source` | `home_assistant` (standaard) of `energy_charts_nl` |
| `price_choice` | `bare` of `all_in` |
| `supplier` | leverancierssleutel uit `supplier_presets`, of `custom` |
| `energy_tax_eur_kwh` | energiebelasting excl. btw in EUR/kWh |
| `vat_percent` | btw als percentage |
| `supplier_fee_eur_kwh_incl_vat` | inkoopvergoeding incl. btw in EUR/kWh |
| `tariff_valid_from`, `tariff_valid_to` | ISO-datums `YYYY-MM-DD`, beide inclusief |
| `supplier_fee_confirmed` | boolean; Tibber vereist `true` voor all-in (contractcontrole) |

Bij `energy_charts_nl` is `tariff_entity` optioneel en zijn `tariff_unit` en `price_field` niet van toepassing. Bij Home Assistant blijven die velden en de bestaande HA-interpretatie gelden. De marktmodus pollt maximaal elke vijf minuten, ook als het modelinterval langer is. Ontbrekende prijzen blijven `missing`; ze worden niet gevuld met modelvoorspellingen als bekende marktprijzen.

## MQTT

Als MQTT is ingeschakeld en Energy-Charts als bron is geselecteerd, publiceert de app twee extra Discovery-sensoren, altijd in `EUR/kWh`. In HA-bronmodus wist de app die marktwaarden; HA-included/excluded bedragen worden niet als ruwe marktprijs herverpakt.

| Discovery entity key | Betekenis | State |
| --- | --- | --- |
| `current_market_price` | actuele kale Energy-Charts-prijs | `stroomvoorspeller/sensor/current_market_price` |
| `current_all_in_price` | actuele afnameprijs met expliciete componenten | `stroomvoorspeller/sensor/current_all_in_price` |

De retained Discovery-configuratie stelt `expire_after` per update in op de resterende seconden tot het einde van het actuele kwartier (1–900 seconden); de polling wordt ook op die UTC-kwartiergrens gewekt. Prijsstates worden niet-retained gepubliceerd. De waarde bestaat alleen wanneer de bron recent is opgehaald, het huidige kwartier exact in de feed staat en de bronwaarde al beschikbaar was. Ontbrekende, verlopen of ongeldige prijs publiceert `None` (unknown); na de intervalgrens verloopt de sensor ook als een update uitblijft. Attributen vermelden intervalstart/einde, bron, kwaliteit en `fetched_at`. Modelprognoses en oude kwartieren vullen deze sensoren nooit.

## Bronnen

- [Energy-Charts API](https://api.energy-charts.info/): NL-biedzone, prijsendpoint, EUR/MWh, kwartier-starttijden, CC BY 4.0 en bronattributie.
- [Zonneplan dynamisch energiecontract](https://www.zonneplan.nl/energie/dynamisch-energiecontract): €0,02/kWh inclusief btw en prijsopbouw.
- [Tweakers, eerste post](https://gathering.tweakers.net/forum/list_messages/2049754): Nederland particulier, peildatum 04-08-2026, variabele stroomopslagen inclusief 21% btw.
- [Tarieven energiebelasting 2026](https://open.overheid.nl/documenten/c3d48c04-1009-4ffe-a54b-cebf13427ab5/file): €0,09161/kWh excl. btw tot 10.000 kWh.

Deze componenten geven een indicatieve prijs per afgenomen kWh. Vaste maand- en netwerkkosten, vermindering energiebelasting, saldering en terugleververgoeding zijn niet inbegrepen.

## EpexPredictor naar een HA-leverancierstarief

Bij Home Assistant als prijsbron kiest de gebruiker onder EpexPredictor een tariefprofiel:

- **Zonneplan — afname incl. belasting en btw (2026)**: `marktprijs EUR/kWh × 1,21 + 0,1308481`. De vaste opslag is `0,09161 × 1,21 + 0,02`. De server vult beide waarden en de omrekenbevestiging in; de HA-prijsreeks moet inclusief belasting zijn. Geldig van 1 januari t/m 31 december 2026, beoordeeld in Nederlandse tijd. Buiten deze periode wordt geen prognose met dit profiel gemaakt. Niet voor teruglevering of afwijkende contracten.
- **Overige leveranciers uit Tweakers**: opslag inclusief btw uit de tabel, peildatum 04-08-2026. Bijvoorbeeld Tibber: `marktprijs × 1,21 + 0,1356481`. Deze nieuwe voorstellen worden alleen toegepast van 4 augustus t/m 31 december 2026; controleer je contract.
- **Eigen tarief / andere leverancier**: de bestaande handmatige vermenigvuldiger, opslag en bevestiging. Bestaande installaties blijven bij deze keuze en behouden hun waarden; de app leidt nooit zelf een leverancier af uit een entiteitsnaam.

`epex_tariff_profile` is `custom` (standaard), `zonneplan_2026` of een leverancierssleutel met suffix `_2026_08` (bijvoorbeeld `tibber_2026_08`). Nieuwe Epex-profielen zijn begrensd tot 4 augustus t/m 31 december 2026. `GET /api/settings` geeft de beschikbare profielen in `epex_tariff_profiles` terug. De backend bepaalt de presetwaarden ook als een client andere getallen meestuurt. De profielkeuze maakt deel uit van de Epex-modelidentiteit. Energy-Charts gebruikt de eigen tariefcomponenten hierboven; het HA-profiel verandert die prijsroute niet.

[Enever-prijzenfeeds](https://enever.nl/prijzenfeeds/) (geraadpleegd 20 oktober 2026) documenteert leverancierscodes en consumentenprijzen inclusief btw, maar geen afzonderlijke leveranciersopslagen. De feeds vereisen een persoonlijk token en zijn bedoeld voor persoonlijk gebruik; publieke herpublicatie is niet toegestaan. Daarom bevat de add-on geen gekopieerde leveranciersprijzen, API-token of automatische Enever-koppeling. De leveranciersvoorstellen komen nu uit de expliciet gedateerde Tweakers-tabel, zonder runtime-forumverzoeken. Eigen tarief blijft beschikbaar.


## Meegeleverde leveranciersopslagen

Alle bedragen hieronder zijn **EUR/kWh inclusief 21% btw**, peildatum 04-08-2026. Oude, doorgestreepte tarieven en de gas- en maandbedragen zijn niet overgenomen. Een toekomstige wijziging van de tabel verandert bestaande installaties niet automatisch.

| Leverancier | Opslag incl. btw |
| --- | ---: |
| ANWB Energie | 0.020000 |
| Budget Energie | 0.016820 |
| Clean Energy | 0.018150 |
| Coolblue Energie | 0.020000 |
| easyEnergy | 0.021780 |
| Eneco | 0.022570 |
| Energiedirect | 0.020500 |
| Energiek | 0.018000 |
| Energie VanOns | 0.029000 |
| EnergyZero | 0.022420 |
| Essent | 0.025280 |
| Frank Energie | 0.018150 |
| GroeneStroomLokaal | 0.022420 |
| Hegg Energy | 0.022420 |
| Innova | 0.025080 |
| NextEnergy | 0.021900 |
| Pure Energie | 0.019990 |
| Quatt | 0.018150 |
| SamSam | 0.022420 |
| Tibber | 0.024800 |
| Vandebron | 0.025710 |
| Vattenfall | 0.025495 |
| Vrijopnaam | 0.017500 |
| ZonderGas | 0.022420 |
| Zonneplan | 0.020000 |
