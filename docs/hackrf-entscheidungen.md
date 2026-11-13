# HackRF-Unterstützung – Entscheidungen (13.11.2026)

**Nur offizielle Software von Great Scott Gadgets.** Firmware aus dem Release v2026.01.3, Windows-Werkzeuge aus dem
offiziellen CI-Build „hackrf-tools-windows“ (laut GSG-Installationsanleitung der Weg für Windows-Binaries), Update
nur mit `hackrf_spiflash` ohne `-i`. Verworfen: eigene Firmware (wie beim Pluto) – der HackRF hat keinen Prozessor für
Modulatoren, und eigene Firmware wäre ein Supportrisiko.

**ctypes auf libhackrf statt `hackrf_transfer -t -`.** Pegel, Frequenz und Not-Aus müssen live gehen; beim Werkzeug
ginge das nur durch Neustart. Verworfen: SoapySDR (schwer, zusätzliche Abhängigkeit).

**Gleiche Schnittstelle wie `pluto.Pluto` statt Abstraktionsschicht.** Der Server spricht beide Geräte über
`self.pluto` an; nur Gerätepfad, Agent, SSH und Kontrollempfang sind Pluto-spezifisch und dort abgefragt.
Verworfen: `sdr.py` mit Basisklasse – mehr Umbau ohne Nutzen bei zwei Geräten.

**Interpolation auf ≥ 8 MS/s** (GSG sampling_rate.rst: unter 8 MS/s nicht empfohlen, Basisbandfilter min. 1,75 MHz).
Ganzzahliger Faktor, Polyphasen-FIR mit Zustand über Blöcke (scipy.signal.upfirdn).

**HF-Verstärker nie an, Dämpfung 0…47 dB.** Entspricht der Pluto-Regel „nie lauter als vorgesehen“; Werte über 47 dB
werden auf 47 dB begrenzt (Warnung), 89,75 dB bleibt „stumm“.

**Standards ohne PC-Modulator gesperrt** (PAL-Schleife, DVB-T2, ATSC, ISDB-T, DTMB, J.83 B/C, Rundfunk-DVB-S/S2).
Offener nächster Schritt: Modulatoren aus `pluto_fw/mod` zusätzlich für x86 in WSL bauen und per USB-Pfad nutzen.
