# Re-Pseudonymisierung von FHIR-CSV-Exporten

Dieser Branch enthält zwei Python-Skripte, mit denen sich CSV-Exporte von
FHIR-Ressourcen einfach und schnell neu pseudonymisieren lassen, ohne dass dafür
ein PSN-Dienst (Pseudonymisierungsdienst) angebunden wird.

| Skript | Aufgabe |
|---|---|
| `transfer_pseudonymize.py` | Ersetzt die IDs durch zufällige UUIDs und speichert die Zuordnung von Original-ID zu UUID. |
| `revert_pseudonymize.py` | Ersetzt die UUIDs anhand der gespeicherten Zuordnungen wieder durch die Original-IDs. |

## Voraussetzungen

- Python 3.9 oder neuer
- [pandas](https://pandas.pydata.org/) (Installation mit `pip install pandas`)
- CSV-Dateien in UTF-8-Kodierung

## Was wird ersetzt?

Die Skripte ersetzen zwei Arten von Werten:

- die Werte der Spalte **`id`**, also die eigene ID jeder Ressource, und
- die IDs in **FHIR-Referenzen** der Form `Ressourcentyp/<id>`, unabhängig davon,
  in welcher Spalte sie stehen. Berücksichtigt werden alle Ressourcentypen von
  FHIR R4. Aus `Patient/123` wird beispielsweise `Patient/<neue UUID>`.

Alle übrigen Werte bleiben unverändert. Das gilt auch für leere Felder und für
Texte wie `NA` oder `null`. Das Trennzeichen (`,`, `;`, Tabulator oder `|`)
wird für jede Datei automatisch erkannt und beim Schreiben beibehalten.

## Verwendung

### 1. Pseudonymisieren

```
python transfer_pseudonymize.py --input-dir <Eingabeordner> --output-dir <Ausgabeordner> --mapping-dir <Zuordnungsordner>
```

- `--input-dir`: Ordner mit den CSV-Dateien, zum Beispiel mit einem Unterordner
  je Standort. Unterordner werden mit durchsucht.
- `--output-dir`: Ordner für die pseudonymisierten Dateien. Die Ordnerstruktur
  entspricht der des Eingabeordners.
- `--mapping-dir`: Ordner für die Zuordnungen. Für jeden Ordner mit CSV-Dateien
  entsteht dort eine Datei `id_pseudonyms.json`.

### 2. Zurückführen

```
python revert_pseudonymize.py --input-dir <Ordner mit pseudonymisierten Daten> --output-dir <Ausgabeordner> --mapping-dir <Zuordnungsordner> [<weitere Zuordnungsordner> ...]
```

Das Skript durchsucht alle angegebenen Zuordnungsordner samt Unterordnern nach
Dateien `id_pseudonyms.json` und führt sie zusammen. Die Ordner mit den Daten
müssen deshalb nicht so heißen wie die Ordner mit den Zuordnungen, was zum
Beispiel bei umbenannten Standortordnern hilft.

### Beispiel

Ausgangslage ist ein Ordner mit einem Unterordner je Standort:

```
daten/
├── standort_a/
│   ├── Patient.csv
│   └── Observation.csv
└── standort_b/
    ├── Patient.csv
    └── Observation.csv
```

Pseudonymisieren:

```
python transfer_pseudonymize.py --input-dir daten --output-dir export --mapping-dir zuordnungen
```

Ergebnis:

```
export/                         <- pseudonymisierte Daten zur Weitergabe
├── standort_a/
│   ├── Patient.csv
│   └── Observation.csv
└── standort_b/
    ├── Patient.csv
    └── Observation.csv

zuordnungen/                    <- NICHT weitergeben
├── standort_a/
│   └── id_pseudonyms.json
└── standort_b/
    └── id_pseudonyms.json
```

Zurückführen:

```
python revert_pseudonymize.py --input-dir export --output-dir zurueckgefuehrt --mapping-dir zuordnungen
```

Der Ordner `zurueckgefuehrt/` hat dieselbe Struktur wie `export/` und enthält
wieder die Original-IDs.

### Option für beide Skripte

- `--separator <Zeichen>`: Gibt das Trennzeichen für alle Dateien fest vor, statt
  es automatisch zu erkennen. Für einen Tabulator lautet die Angabe
  `--separator "\t"`.

## Wichtige Hinweise

- **Zuordnungen schützen:** Die Dateien `id_pseudonyms.json` enthalten die
  Original-IDs. Sie dürfen deshalb nicht mit den pseudonymisierten Daten
  weitergegeben werden. Dasselbe gilt für die Ausgabe von `revert_pseudonymize.py`.
- **Neue UUIDs bei jedem Lauf:** Wird `transfer_pseudonymize.py` erneut
  ausgeführt, überschreibt es die Ausgabe und die Zuordnungen und vergibt neue
  UUIDs. Zum Zurückführen müssen Daten und Zuordnungen daher aus demselben Lauf
  stammen. Dateien im Ausgabeordner, zu denen es keine Eingabedatei mehr gibt,
  werden nicht gelöscht.
- **Zuordnung je Ordner:** Beim Pseudonymisieren gilt eine Zuordnung nur
  innerhalb eines Ordners. Dieselbe Original-ID erhält in verschiedenen Ordnern
  also verschiedene UUIDs.
- **Zuordnung nur über die ID:** Die Zuordnung berücksichtigt nur die ID, nicht
  den Ressourcentyp. Haben zum Beispiel ein Patient und eine Observation dieselbe
  Original-ID, erhalten beide dieselbe UUID. Das ist unkritisch, weil sie schon
  vorher dieselbe ID hatten und Referenzen den Ressourcentyp immer mitführen.
- **Annahmen zum Aufbau der Daten:**
  - Die ID-Spalte heißt `id`.
  - Als Referenz gilt nur ein Wert, der mit `Ressourcentyp/` beginnt.
    Referenzen innerhalb eines längeren Textes oder als vollständige URL (zum
    Beispiel `http://…/Patient/123`) werden nicht ersetzt. Umgekehrt wird jeder
    Wert dieser Form ersetzt, auch wenn er in einer Freitextspalte steht.
  - Versionierte Referenzen wie `Patient/123/_history/2` werden nicht
    unterstützt.
- **Übersprungene Dateien:** Dateien, die sich nicht lesen lassen (zum Beispiel
  wegen einer falschen Kodierung oder eines nicht erkennbaren Trennzeichens),
  werden übersprungen. Am Ende des Laufs listet eine Warnung alle betroffenen
  Dateien auf.

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Alle Dateien wurden vollständig verarbeitet. |
| 1 | Die Ausgabe ist unvollständig, weil mindestens eine Datei übersprungen wurde. Bei `revert_pseudonymize.py` gilt das auch, wenn Werte ohne Zuordnung blieben oder die Zuordnungen nicht lesbar oder widersprüchlich waren. |
| 2 | Der Aufruf war ungültig, zum Beispiel wegen eines fehlenden Arguments oder eines nicht vorhandenen Ordners. Bei `revert_pseudonymize.py` gilt das auch, wenn keine `id_pseudonyms.json` gefunden wurde. |

Weitere Details stehen im Kopfkommentar des jeweiligen Skripts.