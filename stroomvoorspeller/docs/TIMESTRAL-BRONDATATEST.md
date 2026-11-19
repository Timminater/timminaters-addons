# Invloed van brondata op Timestral-123M

Lokale proef van 19 november 2026 op dezelfde zeven historische uitgiftes. De tabelwaarden zijn de gemiddelde absolute fout (MAE) in ct/kWh, gewogen per uitgifte-kwartierpaar. Lagere waarden zijn beter.

## Afzonderlijke weersignalen en weekendinformatie

| Weer naast de prijshistorie | Zonder weekend | Met weekend | Winst door weekend |
|---|---:|---:|---:|
| geen weer | 7,253 | 7,273 | -0,020 |
| zon | 7,481 | 7,316 | 0,165 |
| wind | 4,511 | 4,506 | 0,005 |
| temperatuur | 7,182 | 7,134 | 0,048 |
| zon + wind | 4,558 | 4,534 | 0,024 |
| zon + temperatuur | 7,320 | 7,223 | 0,097 |
| wind + temperatuur | 4,756 | 4,773 | -0,017 |
| zon + wind + temperatuur | 4,700 | 4,695 | 0,004 |

De zonreeks is kortgolvige straling in W/m², wind is windsnelheid op 10 m in m/s en temperatuur is luchttemperatuur op 2 m in °C. Steeds is het ongewogen gemiddelde over de tien ingestelde locaties gebruikt. De weekendcode geldt voor zaterdag en zondag in Nederlandse tijd.

## Extra kalenderkenmerken

| Kalender bij zon, wind en temperatuur | MAE |
|---|---:|
| Geen | 4,700 |
| Weekend | 4,695 |
| Dag van de week | 4,716 |
| Weekend + lokaal uur | 4,914 |
| Dag van de week + lokaal uur | 4,961 |

## Aantal weerlocaties bij gelijke GFS-invoer

| Locaties | Alleen wind + weekend | Zon, wind, temperatuur + weekend |
|---|---:|---:|
| 1 · West, regio Amsterdam | 5,663 | 5,943 |
| 4 · Nederlands gebied, inclusief Noordzee | 5,586 | 5,654 |
| 10 · Nederlands gebied en buurlanden | 5,224 | 5,337 |

De live-opslag bevatte gebiedsgemiddelden. Voor alle drie de geografische varianten is daarom dezelfde historische GFS-uitgifte gebruikt, minimaal twaalf uur voor de prijsuitgifte. De invoer vóór het begin van die weerrun komt uit het historische GFS-archief. Een ontbrekend historisch uur is voor alle drie de gebieden op dezelfde manier gemaskeerd. Het toekomstige weer komt uitsluitend uit de gekozen oude run. Vergelijk de geografische rijen onderling. [Open-Meteo Single Runs](https://open-meteo.com/en/docs/single-runs-api) · [Open-Meteo Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api)

## Invloed van één kenmerk weglaten uit de actieve variant

| Weggelaten kenmerk | Toename MAE (negatief is verbetering) | Uitgiftes waarop weglaten helpt |
|---|---:|---:|
| Zon | 0,078 | 4 / 7 |
| Wind | 2,527 | 0 / 7 |
| Temperatuur | -0,161 | 6 / 7 |
| Weekend | 0,004 | 3 / 7 |

## Fout per uitgiftedag

| Uitgiftedatum | Paren | zon + wind + temperatuur + weekend | wind + weekend | volledig weer + weekdag |
|---|---:|---:|---:|---:|
| 2026-10-23 | 624 | 7,726 | 7,465 | 7,607 |
| 2026-10-26 | 624 | 5,095 | 4,023 | 5,015 |
| 2026-10-30 | 624 | 2,624 | 2,594 | 2,680 |
| 2026-11-02 | 624 | 2,062 | 2,730 | 2,312 |
| 2026-11-05 | 624 | 4,867 | 4,768 | 4,821 |
| 2026-11-09 | 624 | 6,332 | 6,147 | 6,325 |
| 2026-11-12 | 623 | 4,161 | 3,815 | 4,254 |

Alle varianten gebruiken dezelfde historische prijscontext en dezelfde later bekende tarieven. Al bij uitgifte bekende prijzen zijn uitgesloten. Twee van de oorspronkelijke weeropnames droegen een ophaaltijd ná de modeluitgifte. Daarvoor is de eerder opgeslagen verwachting van hetzelfde gebied gebruikt. De meting omvat de onbekende kwartieren tot zeven dagen vooruit. Overlappende doelen en zeven uitgiftedagen in de herfst beperken de generaliseerbaarheid.

Er was voor deze uitgiftedagen geen tijdig opgeslagen NED-restvraag of landelijke Energy-Charts-verbruiks-, zon- of windprognose op deze databasekopie. Die reeksen krijgen pas een score na voldoende opgeslagen uitgiftes met bijbehorende tarieven.

## Controle op de overige veertien uitgiftes

Drie varianten met weekendinformatie zijn ook op de overige veertien volwassen uitgiftedagen uit dezelfde periode vergeleken. Dit zijn 8.732 aanvullende paren. De doelkwartieren kunnen overlappen met die uit de eerste proef. Het is een gevoeligheidscontrole binnen dezelfde periode.

| Weersignalen | Eerste 7 uitgiftes | Overige 14 | Alle 21 | Uitgiftes beter dan volledig weer (van 21) |
|---|---:|---:|---:|---:|
| wind | 4,506 | 4,728 | 4,654 | 17 |
| zon + wind | 4,534 | 4,699 | 4,644 | 19 |
| zon + wind + temperatuur | 4,695 | 5,188 | 5,024 | referentie |
