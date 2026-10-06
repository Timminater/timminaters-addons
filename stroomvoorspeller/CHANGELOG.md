# Changelog

## 0.1.7

- Nederlandse Energy-Charts-marktprijzen als onafhankelijke bron, zonder Zonneplan-integratie.
- Keuze voor kale beursprijs of all-in-afnameprijs met instelbare leverancier, energiebelasting, btw, inkoopvergoeding en geldigheidsperiode.
- Zonneplan-tariefvoorstel voor 2026, Tibber met gecontroleerde handmatige vergoeding, en eigen tarief.
- Twee extra MQTT-sensoren bij de marktbron: huidige kale prijs en huidige all-in-prijs in EUR/kWh, met bronmetadata en verval op de kwartiergrens.
- Gescheiden prijsarchieven en modelruns per tariefconfiguratie; beperkte actuele marktsnapshots houden opslaggebruik beheersbaar.
- Bestaande Home Assistant-prijsbron blijft beschikbaar.

All-in betreft afname per kWh; vaste maandkosten, netbeheerkosten, saldering en terugleververgoeding zijn niet inbegrepen.
