# Changelog

## 1.3.0

- Concept 6 toegevoegd als App-icoon en lokaal Home Assistant integration-brand.
- Backend-URL en companion-token zijn zichtbaar configureerbaar en worden vóór opslaan gevalideerd.
- Selecteerbare conversation-proxy rond een bestaande Home Assistant conversation-agent toegevoegd.
- Optionele enrollmentkoppeling met een Home Assistant `person.*`-entiteit toegevoegd.
- Herkenningsresultaten zijn kortlevend, satellietgebonden en worden maximaal één keer voor personalisatie gebruikt.
- De oorspronkelijke Home Assistant-gebruiker en rechtencontext worden nooit door een stemmatch gewijzigd.

## 1.2.0

- Enrollment via een bestaand Home Assistant Voice-/Assist Satellite-apparaat toegevoegd.
- De App ontdekt beschikbare satellieten en start een veilige STT-only `ask_question`-opname.
- Enrollmentspraak bereikt nooit de conversation- of intent-laag en kan dus geen commando uitvoeren.
- Opnames worden alleen geaccepteerd als de gekozen satelliet de enige luisterende satelliet is.
- Sessies zijn atomisch, begrensd en verlopen automatisch; ruwe opname blijft alleen kort in het geheugen.

## 1.1.0

- Meegeleverde Home Assistant custom integration wordt automatisch geïnstalleerd en bijgewerkt.
- Supervisor-discovery laat de actieve App als ontdekt verschijnen.
- Beveiligde App-koppeling, STT-proxy en herkenningsevent zonder gebruikersrechten te wijzigen.
- Oorspronkelijke WAV/PCM-, samplerate-, gedeelde-resultaat- en API-problemen opgelost.

## 1.0.0

- Eerste Timminater-release op basis van EuleMitKeule/speaker-recognition.
- Ingress-GUI voor upload, microfoon, lijst, verwijderen en herkenningstest.
- Persistente profielen die na restart automatisch worden geladen.
- Veilige profiel-ID's, begrensde PCM-input en atomische opslag.
- Multi-sample aggregatie, append/replace en configureerbare unknown-drempel.
- Ingress-authenticatie en optionele bearer-token voor directe API-toegang.
