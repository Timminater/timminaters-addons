# Architectuur en gegevensgrenzen

## Runtime

Supervisor start de container uit de lokale App-map. Ingress routeert alleen de interne poort 8099 naar de zijbalkpagina; `panel_admin: true` beperkt de zichtbaarheid tot beheerders. De Python-backend serveert de statische pagina en JSON-routes. Zij leest Home Assistant Core via `http://supervisor/core/api/` met `SUPERVISOR_TOKEN`, zoals de [officiële documentatie](https://developers.home-assistant.io/docs/apps/communication/) voorschrijft. Er zijn geen HA-serviceaanroepen of apparaatcommando's.

De lokale database onder `/data` bevat instellingen, kwartierarchief per entiteit, bronsnapshots, voorspellingsruns en resultaten. UTC-start en exclusieve UTC-eind vormen de intervalidentiteit. `Europe/Amsterdam` is alleen voor kalenderfactoren en presentatie. Daardoor blijven de twee kwartierblokken rond de wintertijdomslag uniek.

## Invoer en herkomst

- **Tarief:** geselecteerde HA-sensor. De huidige toestand en gepubliceerde `forecast` vormen bekende kwartieren. History levert historische toestandswisselingen in begrensde tijdvakken. Een ontbrekend of onbruikbaar attribuut geeft een gerichte fout.
- **Weer:** Open-Meteo of gekozen HA-verwachtingsentiteiten. HA-velden en eenheden worden op bruikbaarheid voor zeven dagen gevalideerd. De Home-locatie uit de actuele HA-configuratie is alleen een voorstel in de instellingen; pas na opslaan daarvan gebruikt de App die coördinaten voor Open-Meteo. Geen waarde uit de onderzochte HA-installatie wordt ingebouwd.
- **Model:** lokale berekening. Uurgemiddelden zijn hooguit een afzonderlijke, afgeleide basis uit vier werkelijk bekende kwartieren. Zij zijn geen exact kwartierarchief.

De App schrijft naar haar eigen database. Alle uitgaande verzoeken naar Home Assistant Core zijn GET. Alleen bij Open-Meteo-keuze wordt een externe weer-GET gedaan. De historische marktbron wordt optioneel via GET geraadpleegd. Wanneer de gebruiker MQTT-entiteiten inschakelt, publiceert de App berekende prijzen en datastatus naar de door Supervisor aangeboden MQTT-broker. Zij verstuurt geen apparaatcommando's. Een lokale browser moet via Ingress openen om de JSON-routes te bereiken.

## Bewijsgrens

De upstream uurmethode en de nieuwe kwartieradapter hebben gescheiden tests. Walk-forward backtests gebruiken uitsluitend invoersnapshots die bij `issued_at` beschikbaar waren. MAE, bias, dekking en vensterkeuze worden per horizon naast een eenvoudige kwartierbasislijn gezet. Een dagelijkse vergelijking telt voor rijping pas mee als ten minste 92 van de eerste 96 voorspelde kwartieren achteraf een echte prijs én een causale kwartierbasislijn hebben. Na zeven zulke verschillende dagen en 35 dagen met ten minste 95% archiefdekking heet de reeks **lokaal geëvalueerd**. Dat is geen nauwkeurigheidsclaim: de pagina toont de gemeten fouten en vermeldt dat de kwartierband niet gekalibreerd is. Daarvóór blijft het label **voorlopig**.

Het Analyse-tabblad gebruikt opgeslagen voorspelde kwartierprijzen en achteraf bekende, werkelijk gemeten tariefprijzen uit dezelfde entiteit-, prijsveld- en eenheidsreeks. Per lokale dag wordt één representatieve run gekozen. Een empirische bandbreedte mag pas worden getoond na voldoende oudere onafhankelijke dagen; evaluatie van de dekking gebruikt latere runs dan de kalibratiegegevens. Het aantal kwartieren en afzonderlijke dagen wordt bij de uitkomst vermeld. Een gemeten dekking uit het verleden is geen garantie voor toekomstige dekking.
