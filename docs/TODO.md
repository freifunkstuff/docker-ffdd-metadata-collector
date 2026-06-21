
# Status-abhängige Fetch-Zeiten
* früher:
    Unbekannte Community (d.h. neu, Metadaten nicht bekannt): jede 1 Minute
    Leipzig-Nodes: alle 5 Minuten
    Andere Communities: alle 15 Minuten
* prüfen, ob wir das noch brauchen

# Kartendaten direkt erzeugen

Alte Java-Basis:
- Es gab bereits eine direkte Meshviewer-Ausgabe mit `nodes` und `links`
- Der alte Endpoint war effektiv Leipzig-gefiltert plus Server/Gateways
- Knoten wurden für Meshviewer aus Node-Info, Node-Stats und Link-Daten zusammengesetzt

Was wir jetzt schon haben:
- Persistenten Gesamtzustand aller bekannten Nodes im Python-Collector
- Snapshot-Ausgabe mit `nodes` und zusammengeführten `links`
- In `info` und `stats` stecken die meisten Rohfelder für Meshviewer bereits drin: Name, Community, Gruppe, Modell, Firmware, Autoupdater, Kontakt, Geo-Daten, Clients, Uptime, Load, Speicher
- Community steht bereits normalisiert zur Verfügung
- Link-Daten sind pro Node bereits im Speicher und werden im Snapshot zusammengeführt

Was noch fehlt:
- Eigene Transformation vom Collector-Snapshot auf das Meshviewer-Format
- Explizite Regeln für Online/Stale/Hidden analog zum alten Meshviewer-Verhalten
- Ableitung der Meshviewer-Felder, die heute noch nicht direkt fertig vorliegen: `mac`, `hostname`-Darstellung, `memory_usage`, Link-Typ-Mapping, TQ-Normalisierung
- Klären, welche Nodes in die Kartendaten dürfen: nur mit `info`, nur mit gültiger Location auf der Karte, Gateways ohne Location weiterhin in den Daten
- Klären, ob wir das alte Stale-Verhalten für temporäre Nodes und sehr alte Nodes übernehmen wollen

Zielartefakte:
- Gesamt-Meshviewer für alle Communities
- Meshviewer pro Community

Vorschlag für Ausgabe:
- Ein globales Artefakt, zum Beispiel `meshviewer/meshviewer.json`
- Zusätzlich je Community ein eigenes Artefakt, zum Beispiel `meshviewer/by-community/<community>/meshviewer.json`
- Links pro Community nur dann ausgeben, wenn beide Enden in derselben Community-Ausgabe enthalten sind
- Server/Gateways für die jeweilige Community-Ausgabe weiter zulassen, auch wenn sie keine Koordinaten haben

Offene Designfragen:
- Gesamtdatei nur als Union aller Community-Dateien oder als eigene unabhängige Sicht erzeugen
- Community-Schlüssel: Originalname aus `info.community` oder normalisierter Slug im Pfad
- Nur ein kombiniertes `meshviewer.json` oder zusätzlich getrennte `nodes.json` und `graph.json` für spätere Meshviewer-Kompatibilität
- Erzeugung direkt im Snapshot-Schritt oder als eigener periodischer Export-Schritt

# Statistik nach VictoriaMetrics pushen

Java pusht nach jedem erfolgreichen Node-Fetch an `POST /api/v1/import/prometheus`.
Link-Metriken werden alle 5 Min als Batch gepusht.

## Node-Metriken

Labels (gleich für alle): nodeid, hostname, group, model, domain, owner, autoupdater, firmware_base, firmware_release
(aus `info`: name→hostname, community→domain, contact_email→owner, auto_update→autoupdater "enabled"/"disabled")

| Metrik | stats-Feld |
|---|---|
| node_info | 1 (Marker) |
| node_time.up | uptime_seconds |
| node_traffic.rx.bytes | traffic_wifi_rx |
| node_traffic.tx.bytes | traffic_wifi_tx |
| node_clients.wifi24 | clients_2g |
| node_clients.wifi5 | clients_5g |
| node_clients.total | clients_2g + clients_5g |
| node_load | load_avg_5 |
| node_memory.total | mem_total |
| node_memory.available | mem_free |
| node_memory.airtime_{2g,5g}_{busy,active,rx,tx} | airtime_* |

## Link-Metriken (Batch, alle 5 Min)

| Metrik | Beschreibung |
|---|---|
| link_tq | TQ-Wert mit Timestamp, nur wenn < 15 Min alt |

Labels: source.id, source.hostname, target.id, target.hostname

## Umsetzung

- Push vom Fetch entkoppeln: kein Export mehr direkt in `_poll_node()`
- Eigener Metrics-Loop mit fixem Intervall von 5 Minuten
- Pro Tick aktuellen Metrik-Stand aus dem Speicherzustand bauen: Node-Metriken und Link-Metriken gemeinsam in einem Lauf
- Vor dem Push prüfen, ob sich seit dem letzten erfolgreichen Export etwas geändert hat
- Änderungsquelle zunächst einfach halten: letzte relevante Aktualisierung aus Node-/Link-Daten mitführen oder Export-Hash vergleichen
- Nur bei Änderungen an VictoriaMetrics pushen, sonst den Tick ohne Request beenden
- Neues Modul `metrics_exporter.py`: MetricsBuilder + async HTTP POST (urllib reicht, keine neue Dep)
- Config: `METADATA_COLLECTOR_VICTORIAMETRICS_URL` (optional, None = aus)
- Community-Filter: `METADATA_COLLECTOR_METRICS_COMMUNITIES`
- Zusätzliche Export-State-Datei oder In-Memory-State für `last_exported_at` beziehungsweise letzten Payload-Hash prüfen
