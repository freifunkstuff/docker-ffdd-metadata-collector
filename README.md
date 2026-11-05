# FFDD Metadata Collector

Erweiterungsimage auf Basis von `ghcr.io/freifunkstuff/ffdd-node:v1.0.1`.
Die Basis übernimmt Identität, Registrierung, Mesh-Anbindung, Sysinfo und Web-UI.
Der Collector liest standardmäßig die lokale Knotenliste aus
`/run/freifunk/sysinfo/nodes.json`, fragt Sysinfo der Knoten ab und erzeugt
Metadaten, Meshviewer-Dokumente und optional VictoriaMetrics-Metriken.

## Bauen und testen

```sh
python3 -m unittest discover -s tests -v
docker build -t ffdd-metadata-collector:local .
```

Der Docker-Build führt die Tests in einer separaten Stage aus. Darunter sind
Producer/Consumer-Tests gegen den tatsächlich im Basis-Image installierten
Knotenlisten-Producer. Bei lokalen Tests wird ersatzweise der benachbarte
Docker-Node-Checkout verwendet; ohne installierte oder benachbarte Basis werden
nur diese Contract-Tests übersprungen. Im normalen Image-Build ist die Basis
vorhanden. Die Testquellen werden nicht ins finale Image übernommen.

Der Container verwendet die Start-Hooks und Dienstüberwachung der Node-Basis.
Konfigurationswerte stehen in `defaults.yaml` und können durch gleichnamige
Umgebungsvariablen überschrieben werden. Persistenter Zustand liegt unter
`/data/state`; Ausgaben liegen unter `/run/freifunk/state` und werden über
Symlinks im Webroot publiziert.

## Wiederanlauf und Datenqualität

- Die aktuelle Discovery-IP hat beim Laden Vorrang vor älteren Poll-Snapshots.
  Ein verspätetes Ergebnis einer Anfrage an die alte IP kann die inzwischen
  entdeckte neue Adresse nicht zurücksetzen.
- Polling-Deadlines werden explizit gespeichert und beim Neustart wieder geladen;
  ältere Zustände ohne Deadline werden sofort eingeplant. Ungültige Deadlines
  verhindern keinen Start.
- Nicht endliche Zahlen (`nan`, `inf`) und nicht darstellbare Uptime-Werte werden
  beim Parsen beziehungsweise Export verworfen, ohne den ganzen Knoten zu verlieren.
  Nicht endliche Float-Werte aus älteren YAML-Zuständen werden beim JSON-Schreiben
  als `null` veröffentlicht; es werden keine ungültigen `NaN`-Literale ausgegeben.
- Der Meshviewer-Bootzeitpunkt basiert auf Messzeitpunkt minus gemessener Uptime,
  nicht auf der späteren Exportzeit.
- Kontakt-/Knotennamen werden einmal im Parser URL-dekodiert; der Export dekodiert
  normalisierte Namen nicht erneut.
- Fehler beim Erzeugen oder Schreiben von Snapshots werden geloggt und beim
  nächsten Snapshot erneut versucht. Alle gestarteten Ausgabeschreibvorgänge
  werden abgewartet, bevor die nächste Generation beginnt.
- Unerwartete Fehler in Hintergrundloops werden geloggt und die Loops nach kurzer
  Pause neu gestartet. Beendet sich ein Worker trotzdem, bleibt der Hauptprozess
  nicht unbemerkt ohne diesen Worker aktiv.

## Auslieferung

- `/node-metadata.json`
- `/node-metadata-status.json`
- `/meshviewer/meshviewer.json`
- `/meshviewer/<community>/meshviewer.json`
- Collector-Status als Erweiterung der Basis-UI unter `/ui/`

Das Image erbt die verifizierte Registrierungs-TLS-Konfiguration der Node 1.0.1.
Die `v1.0.1`-Basis allein veröffentlicht oder aktualisiert noch keinen laufenden
Collector; dafür muss dieses Erweiterungsimage neu gebaut und ausgerollt werden.
