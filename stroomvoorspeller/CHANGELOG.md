# Changelog

## 0.2.4

- Timestral-modelkeuze: 5M, 28M, 123M of 209M. Nieuwe installaties krijgen 28M als standaard; bestaande 209M-instellingen, bestanden en scores blijven behouden.
- Handmatige download per variant, eigen modelmap en gepinde revisie met SHA-256 voor gewichten en configuratie. Wisselen start geen download.
- Gekozen variant zichtbaar in de methodevergelijking, met aparte scores en kalibratie per variant. Een lopende berekening met het vorige model wordt gestopt.
- Downloadgrootte en licentielink volgen de selectie; toestemming moet opnieuw worden aangevinkt na wisselen.

## 0.2.3

- 25 leveranciersvoorstellen uit de Tweakers-tabel (peildatum 04-08-2026), met expliciete btw-basis en bronvermelding.
- Eén lokale tarieflijst voor EpexPredictor naar HA en Energy-Charts all-in; die all-in-reeks voedt ook grafiek, modelinvoer, evaluatie en MQTT.
- Nieuwe voorstellen begrensd vanaf de peildatum tot eind 2026; geen onbewezen historische tarieven. Bestaande eigen componenten en het Zonneplan-profiel blijven behouden.
- Energy-Charts-leverancierskeuze vult opslag en periode in; Eigen tarief laat componenten aanpassen. Vattenfalls zes decimalen blijven behouden.

## 0.2.2

- Leverancierskeuze voor de Epex-omrekening naar HA-tarieven: Zonneplan-afname incl. belasting/btw met automatische 2026-waarden, of Eigen tarief.
- Bestaande handmatige omrekeningen blijven behouden; het Zonneplan-profiel wordt buiten 2026 geweigerd.
- Omrekenvelden alleen zichtbaar bij Eigen tarief en HA als prijsbron.

## 0.2.1

- EpexPredictor NL en Timestral-209M rekenen volledig in de add-on; externe service-URL's en de losse Timestral-service verwijderd.
- Ingebouwd Epex NL LightGBM-model, brondata-cache en instelbare trainingsperiode van 30–180 dagen.
- Handmatig bevestigde Timestral-modeldownload met status, voortgang, gepinde gewichten en SHA-256-verificatie; inferentie blijft strikt offline.
- Eén lokaal ML-proces tegelijk, 1–4 instelbare CPU-threads en vrijgave van model-RAM na iedere run; uitschakelen stopt lopend ML-werk.
- Ook Epex kan historisch lokaal terugrekenen, apart van live prestatiemetingen.
- Debian slim-basis met ingebouwde CPU-bibliotheken voor amd64 en aarch64; de modelgewichten blijven optioneel onder /data.

## 0.2.0

- Adaptieve lokale kwartiermethode met historische patroonkeuze en niveaucorrectie.
- Afzonderlijke schakelaars voor Lokaal adaptief, oorspronkelijke v4, EpexPredictor NL en Timestral-209M; keuze van actieve methode voor grafiek/MQTT.
- Optionele aparte Timestral CPU-service met tijdelijk inferentieproces en gepinde modelgewichten.
- Gescheiden live/historische methodevergelijking, gepaarde MAE en begrensde handmatige terugrekening op bestaand kwartierarchief.
- Instelbare initiële HA-historie (14–180 dagen); historische reconstructies tellen niet mee voor live kalibratie.
- Gepubliceerde Energy-Charts-kwartieren tellen ook mee als echte prijzen voor evaluatie en archiefdekking.

## 0.1.7

- Nederlandse Energy-Charts-marktprijzen als onafhankelijke bron, zonder Zonneplan-integratie.
- Keuze voor kale beursprijs of all-in-afnameprijs met instelbare leverancier, energiebelasting, btw, inkoopvergoeding en geldigheidsperiode.
- Zonneplan-tariefvoorstel voor 2026, Tibber met gecontroleerde handmatige vergoeding, en eigen tarief.
- Twee extra MQTT-sensoren bij de marktbron: huidige kale prijs en huidige all-in-prijs in EUR/kWh, met bronmetadata en verval op de kwartiergrens.
- Gescheiden prijsarchieven en modelruns per tariefconfiguratie; beperkte actuele marktsnapshots houden opslaggebruik beheersbaar.
- Bestaande Home Assistant-prijsbron blijft beschikbaar.

All-in betreft afname per kWh; vaste maandkosten, netbeheerkosten, saldering en terugleververgoeding zijn niet inbegrepen.
