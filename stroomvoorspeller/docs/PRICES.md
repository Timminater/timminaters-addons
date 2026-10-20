# Marktprijs en all-in keuze

De bestaande bron **Home Assistant-tariefentiteit** blijft de standaard. Met **Energy-Charts Nederland** haalt de app rechtstreeks de Nederlandse day-ahead-reeks op via `https://api.energy-charts.info/price?bzn=NL`. De API meldt bedragen in `EUR / MWh`; de app converteert met delen door 1000 naar `EUR/kWh`. De UTC-starttijd identificeert een kwartier van exact 15 minuten. De bronattributie is Energy-Charts.info (Fraunhofer ISE), CC BY 4.0. De app bewaart het ophaaltijdstip als `published_at` en neemt alleen waarden mee die bij de modelrun al waren opgehaald. De bron wordt maximaal eens per vijf minuten opgevraagd, ruim onder de standaardlimiet van twee verzoeken per minuut.

**Kale beursprijs** is de day-ahead marktwaarde zonder btw, energiebelasting of leveranciersvergoeding. **All-in afnameprijs** wordt berekend als:

```text
(marktprijs EUR/kWh + energiebelasting excl. btw) * (1 + btw/100) + inkoopvergoeding incl. btw
```

De instellingen bewaren leverancier, drie componenten en een inclusieve geldigheidsperiode. De meegeleverde componenten gelden voor 2026: energiebelasting €0,09161/kWh excl. btw voor de huishoudelijke schijf tot en met 10.000 kWh, btw 21%, Zonneplan-inkoopvergoeding €0,02/kWh incl. btw. Voor Tibber staat de vergoeding bij leverancierkeuze leeg totdat de gebruiker de actuele btw-basis controleert en invult; de leverancier noemt €0,0180/kWh, maar publiceert de btw-basis van die inkoopvergoeding niet duidelijk genoeg om stil om te rekenen. Eigen tarieven vereisen handmatig ingevulde componenten en datums. All-in wordt voor kwartieren buiten de geldigheidsdatums niet gemaakt.

Prijsbron, prijskeuze en componenten zijn onderdeel van de interne archief- en modelrunidentiteit. Een wijziging begint daarom een eigen reeks; historische prijzen en voorspellingen van een andere configuratie worden niet hergebruikt. Historische all-in kwartieren buiten de ingevoerde geldigheidsperiode worden weggelaten, niet teruggerekend met actuele componenten.

## Instellingen API

`PUT /api/settings` accepteert, naast de bestaande velden:

| Veld | Waarden / eenheid |
| --- | --- |
| `price_source` | `home_assistant` (standaard) of `energy_charts_nl` |
| `price_choice` | `bare` of `all_in` |
| `supplier` | `zonneplan`, `tibber` of `custom` |
| `energy_tax_eur_kwh` | energiebelasting excl. btw in EUR/kWh |
| `vat_percent` | btw als percentage |
| `supplier_fee_eur_kwh_incl_vat` | inkoopvergoeding incl. btw in EUR/kWh |
| `tariff_valid_from`, `tariff_valid_to` | ISO-datums `YYYY-MM-DD`, beide inclusief |
| `supplier_fee_confirmed` | boolean; Tibber vereist `true` voor all-in |

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
- [Tibber contractvoorwaarden](https://tibber.com/nl/voorwaarden/contractvoorwaarden): ex-btw kwartierprijs is gebaseerd op EPEX plus inkoopvergoeding; de btw-basis van het gepubliceerde marketingbedrag €0,0180 blijft door de gebruiker te bevestigen.
- [Tarieven energiebelasting 2026](https://open.overheid.nl/documenten/c3d48c04-1009-4ffe-a54b-cebf13427ab5/file): €0,09161/kWh excl. btw tot 10.000 kWh.

Deze componenten geven een indicatieve prijs per afgenomen kWh. Vaste maand- en netwerkkosten, vermindering energiebelasting, saldering en terugleververgoeding zijn niet inbegrepen.

## EpexPredictor naar een HA-leverancierstarief

Bij Home Assistant als prijsbron kiest de gebruiker onder EpexPredictor een tariefprofiel:

- **Zonneplan — afname incl. belasting en btw (2026)**: `marktprijs EUR/kWh × 1,21 + 0,1308481`. De vaste opslag is `0,09161 × 1,21 + 0,02`. De server vult beide waarden en de omrekenbevestiging in; de HA-prijsreeks moet inclusief belasting zijn. Geldig van 1 januari t/m 31 december 2026, beoordeeld in Nederlandse tijd. Buiten deze periode wordt geen prognose met dit profiel gemaakt. Niet voor teruglevering of afwijkende contracten.
- **Eigen tarief / andere leverancier**: de bestaande handmatige vermenigvuldiger, opslag en bevestiging. Bestaande installaties blijven bij deze keuze en behouden hun waarden; de app leidt nooit zelf een leverancier af uit een entiteitsnaam.

`epex_tariff_profile` is `custom` (standaard) of `zonneplan_2026`. `GET /api/settings` geeft de beschikbare profielen in `epex_tariff_profiles` terug. De backend bepaalt de presetwaarden ook als een client andere getallen meestuurt. De profielkeuze maakt deel uit van de Epex-modelidentiteit. Energy-Charts gebruikt de eigen tariefcomponenten hierboven; het HA-profiel verandert die prijsroute niet.

[Enever-prijzenfeeds](https://enever.nl/prijzenfeeds/) (geraadpleegd 20 oktober 2026) documenteert leverancierscodes en consumentenprijzen inclusief btw, maar geen afzonderlijke leveranciersopslagen. De feeds vereisen een persoonlijk token en zijn bedoeld voor persoonlijk gebruik; publieke herpublicatie is niet toegestaan. Daarom bevat de add-on geen gekopieerde leveranciersprijzen, API-token of automatische Enever-koppeling. Alleen onafhankelijk gecontroleerde componenten worden als lokaal profiel meegeleverd. De overige leveranciers blijven via Eigen tarief instelbaar.
