# Fork-audit

Audit uitgevoerd op 22 juli 2026 tegen upstream `a5996a0`. Alle acht door GitHub gerapporteerde forks, 26 publiek zichtbare branches en 12 upstream pull requests zijn bekeken: `cyberkov`, `petep0p`, `booyasatoshi`, `anxmez`, `Ilya56`, `jgsaez9`, `HexAbyss` en `archer-developer`.

## Verwerkt

- Correcte multi-sample aggregatie en het daadwerkelijk gebruiken van nieuwe samples: [Ilya56 `63b51f4`](https://github.com/Ilya56/speaker-recognition/commit/63b51f4d998d86603ae369cf0e80e4c0fccef06c), opnieuw geïmplementeerd als genormaliseerd gemiddelde met append/replace.
- Veilige audio-/padverwerking: [Ilya56 `bb84c40`](https://github.com/Ilya56/speaker-recognition/commit/bb84c40587a2113ba93dc9ad38fad78d98b3701b), verder aangescherpt met UUID-bestandsnamen, strict base64, PCM-, duur- en stiltevalidatie.
- Profielen laden na restart en status tonen: [Ilya56 `300b773`](https://github.com/Ilya56/speaker-recognition/commit/300b7735fefeb35e1f33d34f1477c16cd1bce531), verwerkt in startup-load en `/health`.
- Veilige standaard-confidence: [PR #11 / `07e873b`](https://github.com/EuleMitKeule/speaker-recognition/pull/11), verwerkt als configureerbare threshold (`0.65`) met expliciete unknown-uitkomst.
- Trainingfouten zichtbaar maken: [PR #14 / `b9c8e66`](https://github.com/EuleMitKeule/speaker-recognition/pull/14), voor deze App verwerkt als concrete API-fouten en GUI-feedback.
- Build/smoke-testideeën uit [PR #9](https://github.com/EuleMitKeule/speaker-recognition/pull/9), gemoderniseerd naar een direct bouwbare Debian/glibc-container en repository-CI.
- Documentatiefixes uit [cyberkov `bdc3ebd`](https://github.com/cyberkov/speaker-recognition/commit/bdc3ebd9defc4929dc2061dab695977847127c0a) en testideeën uit [`651cb1a`](https://github.com/cyberkov/speaker-recognition/commit/651cb1acf3c04fbefa1e474c00d2f70a228a1618).

## Niet blind overgenomen

- PR #3/Ilya's identity-prefix wijzigt gebruikersprompts en kan identiteit naar downstream agents lekken.
- PR #6 gebruikt per request threads met `asyncio.run`, lekt clients en behandelt WAV-headers als PCM.
- PR #4 triggert modelstatus via een dummy recognition-call; de App heeft expliciete readiness.
- Archer `182492c` laat lage confidence STT blokkeren; een onbekende speaker mag transcriptie niet standaard stoppen.
- Integratiegerichte PR #12/#13 en delen van Archer `ea996de` horen bij de nog niet productierijpe upstream custom integration en zijn daarom niet in deze zelfstandige App opgenomen.
