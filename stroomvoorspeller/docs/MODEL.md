# Model, herkomst en kwartierafwijkingen

## Herkomst en eigen implementatie

De methode verwijst naar [Mr-MIle/stroomvoorspeller](https://github.com/Mr-MIle/stroomvoorspeller)
op commit [`175a0ed975c409fbf9329a62687a7952ee745514`](https://github.com/Mr-MIle/stroomvoorspeller/commit/175a0ed975c409fbf9329a62687a7952ee745514).
`app/forecast_core.py` implementeert alleen de gebruikte v4-rekenregels in een eigen, kleinere structuur. De niet-gebruikte modules uit de referentie maken geen deel uit van deze App. Een andere codevorm alleen bepaalt geen gebruiksrechten; beoordeel herkomst en rechten afzonderlijk vóór publieke publicatie.

## Uurmethode

De lokale rekenkern gebruikt vaste parameters:

- baseline `v4`: mediaan op dezelfde lokale uurpositie, met 25% korte mediaan
  (7 dagen op werkdagen, 14 dagen voor weekend/feestdag) en 75% lange mediaan
  (28 dagen per werkdag/weekendgroep);
- de brontrend is het gemiddelde over 7 tegenover 28 dagen, begrensd op 0,5–2,0
  en tot de macht 0,25 gedempt;
- de actieve factoren zijn `wind`, `gas`, `vorige_dag`, `dagtype`, `nonlinear`,
  `scarcity` en `zomerschaarste`;
- `POINT_WEIGHT=0.015`, `NONLINEAR_FLOOR=-3.0`, `SCARCITY_SCALE=1.5`,
  zomerregime aan en `SUMMER_SCARCITY_SCALE=1.0`;
- de bron-v4 absolute uurband is berekend voor een EPEX-uurmodel. De adapter
  retourneert daarom geen onder- of bovengrens.

De regressietests vergelijken de nieuwe kern met vooraf vastgelegde v4-uitkomsten voor normale, extreme en ontbrekende invoer. Een referentiegeval heeft basislijn 109,46, 11 factorpunten en uitkomst 127,52. De uurband wordt alleen voor rekenpariteit gecontroleerd; zij wordt niet als kwartierband getoond.

## Kwartieradapter

`forecast_quarters(history, issued_at, weather=None, max_points=672,
price_scale=1.0)` neemt uitsluitend kwartierintervallen aan. De opslag- en
intervalidentiteit blijft UTC. De kalenderfactoren gebruiken
`Europe/Amsterdam`; tijdens de wintertijd blijven de twee lokale 02:xx-blokken
gescheiden op basis van hun UTC-offset.

Kwartierwaarnemingen moeten een start, eind, prijs en zo mogelijk
`published_at` bevatten. Een toekomstige prijs is alleen modelinvoer als de
publicatietijd niet later is dan `issued_at`; een toekomstige rij zonder die
publicatietijd wordt uitgesloten. Ongeldige, niet-uitgelijnde of niet-exacte
kwartierintervallen worden genegeerd.

De bron rekent in EUR/MWh. `price_scale` schaalt invoer daarom tijdelijk naar
EUR/MWh en zet de uitkomst terug naar de eenheid van de entiteit. Voor een
entiteit in EUR/kWh is de schaalfactor 1000; voor EUR/MWh is die 1. De App
voorspelt rechtstreeks het gekozen tarief. Dat is een relatieve toepassing van
de bronfactoren en bewijst niet dat de marktcomponent uit een Zonneplan-tarief
kan worden geïsoleerd.

De aanpassingen aan de uurmethode zijn:

1. **Kwartierbasis:** historische kwartierprijzen worden beperkt tot dezelfde
   lokale uur- en kwartierpositie en dezelfde UTC-offset als het doelkwartier.
   De lokale rekenkern berekent daarna de korte en lange mediaan en
   trendfactor. Bij minder dan twee vergelijkbare echte kwartierwaarnemingen
   valt de adapter terug op complete uren die uit vier echte kwartierprijzen
   zijn gemiddeld. Alle voorspelde kwartieren in zo'n uur krijgen dezelfde
   vlakke waarde. Uurgemiddelden worden dus nooit als vier gemeten
   kwartierprijzen behandeld.
2. **Tijdidentiteit:** bronkalenderlogica blijft op Amsterdamse lokale tijd,
   maar kwartieren, invoersnapshots en voorspellingstijdstippen zijn
   offsetbewuste UTC-tijdstippen. Daarmee worden zomer-/wintertijdwissels niet
   samengevoegd.
3. **Weer:** ruwe Open-Meteo-uurlijkse zoninstraling wordt met de
   De-Bilt-maandnorm en uurverdeling uit de bronrunner genormaliseerd.
   Windsnelheid op 10 m wordt net als in de runner met 1,38 naar 100 m
   omgerekend. Voor nachtelijke uren gebruikt de adapter de bronrunner-dagratio;
   als de ruwe invoer die niet bevat, wordt die alleen herberekend uit ten minste
   18 uurwaarden van dezelfde lokale dag. De adapter accepteert al
   genormaliseerde verhoudingen ook rechtstreeks. Bij ontbrekende waarden
   worden neutrale waarden gebruikt (zonratio 1, wind 8 m/s, temperatuur 15 °C,
   gasratio 1) en de run blijft voorlopig met de ontbrekende velden in `reasons`.
   Zonder TTF-invoer is de factor neutraal (`ttf_ratio=1`), met een expliciete
   ontbrekende-inputmelding.
4. **Uitgesloten modules:** biascorrecties, negatieve-prijskans en
   analogie-/eventplausibiliteit worden niet toegepast. Hun uur-EPEX-data,
   drempels en foutgeschiedenis zijn niet aangetoond voor dit tarief en deze
   kwartierresolutie.
5. **Onzekerheid:** er wordt geen kwartierband getoond of gekopieerd uit de
   uurband. Runs blijven `voorlopig` totdat minimaal 35 dagen kwartierdekking
   en zeven volwassen dagelijkse vergelijkingen beschikbaar zijn. Daarna toont
   de pagina `lokaal geëvalueerd` met de gemeten fout, zonder garantie voor
   toekomstige nauwkeurigheid of een gekalibreerde kwartierband.

`model.py` houdt historische modelinvoer strikt point-in-time: de kern filtert
op `published_at <= issued_at`, slaat de run-identiteit op als UTC en stopt na
672 kwartieren. Een latere uitkomst mag daardoor geen eerder uitgegeven
voorspelling wijzigen.

## Bewijsgrenzen

De tests in `tests/test_model.py` controleren vaste uurkernuitkomsten,
plus schaalconversie, future-inputfiltering, fallback naar vlakke kwartieren,
ontbrekende weerinvoer en UTC-afhandeling van beide DST-overgangen. Dit is
implementatiebewijs, geen nauwkeurigheidsbewijs. De walk-forward backtest
gebruikt opgeslagen point-in-time weerssnapshots en latere kwartieruitkomsten.
Zij rapporteert per horizon MAE, bias, de gepaarde vergelijking met een causale
kwartierbasislijn, en beide goedkoopste-vensterkeuzes op dezelfde gerealiseerde
kwartieren. De banddekking blijft leeg zolang er geen kwartierband is.
