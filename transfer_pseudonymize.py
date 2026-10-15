################################################################################
# transfer_pseudonymize.py
#
# Skript zur einfachen und schnellen Re-Pseudonymisierung OHNE Anbindung eines
# komplexen PSN-Dienstes (Pseudonymisierungsdienst).
#
# Das Skript pseudonymisiert CSV-Exporte von FHIR-Ressourcen neu, indem es die
# Spalte `id` und alle IDs in Referenzen der Form `Ressourcentyp/<id>` durch
# zufällige UUIDs ersetzt. Die pseudonymisierten Dateien werden in einen
# Ausgabeordner geschrieben, die Zuordnung von Original-ID zu UUID getrennt
# davon je Ordner als JSON gespeichert.
#
# Ablauf:
# 1. Einlesen der DIZ-Daten, die bereits als CSV vorliegen
# 2. Erzeugen zufälliger Export-IDs (UUID v4) für alle IDs
# 3. Ersetzen der Original-IDs durch die Export-IDs, und zwar
#    - in der Spalte "id" (eigene ID der Ressource) und
#    - in allen Spalten, deren Wert wie eine FHIR-Referenz aussieht,
#      z.B. "Patient/123" -> "Patient/<neue UUID>"
# 4. Schreiben der auslieferungsfertigen CSV-Dateien mit den ersetzten IDs
# 5. Speichern der ID-Zuordnungen (Original-ID -> UUID) als JSON
#
# Aufruf (Python >= 3.9, benötigt pandas):
#   python transfer_pseudonymize.py --input-dir <Eingabeordner> \
#          --output-dir <Ausgabeordner> --mapping-dir <Ordner für ID-Zuordnungen>
# Optional: --separator <Zeichen>, um die automatische Erkennung des CSV-Trennzeichens
# zu überschreiben (für Tabulator: --separator "\t").
#
# Dateien, die nicht gelesen werden können (z.B. falsche Kodierung oder nicht
# erkennbares Trennzeichen), werden übersprungen; am Ende gibt das Skript eine
# Warnung mit allen übersprungenen Dateien aus.
#
# Rückgabewert (Exit-Code):
#   0 = alle Dateien wurden verarbeitet
#   1 = mindestens eine Datei wurde übersprungen (Ausgabe unvollständig)
#   2 = ungültige Aufrufparameter (z.B. fehlendes Argument, ungültiger Ordner)
#
# Trennzeichen: Es wird für jede CSV-Datei einzeln erkannt (",", ";", Tabulator
# oder "|") und beim Schreiben der pseudonymisierten Datei beibehalten.
#
# Wichtig zum Geltungsbereich der Zuordnung:
# - Die Zuordnung (id_map) gilt je Ordner (beispielsweise 1 Ordner pro lieferndem Standort).
#   Alle CSV-Dateien eines Ordners teilen sich dieselbe Zuordnung, sodass Referenzen zwischen
#   diesen Dateien konsistent ersetzt werden. Zwischen verschiedenen Ordnern wird NICHT
#   abgeglichen: dieselbe Original-ID erhält in jedem Ordner eine andere UUID.
# - Die Zuordnung erfolgt rein über die ID, nicht über Ressourcentyp + ID.
#   Haben z. B. Patient und Observation in den Quelldaten dieselbe ID (z.B. "1"),
#   erhalten sie dieselbe UUID. Das ist unkritisch, da bereits vorher dieselbe ID
#   vorlag und FHIR-Referenzen (z. B. "Patient/<UUID>") den Typ stets mitführen.
# - Bei erneuter Ausführung werden Ausgabe und ID-Zuordnungen überschrieben und
#   alle IDs erhalten neue UUIDs (beabsichtigt). Dateien im Ausgabeordner, zu denen
#   es keine Eingabedatei mehr gibt, werden dabei nicht gelöscht.
# - Versionierte Referenzen (z. B. "Patient/123/_history/2") werden nicht unterstützt:
#   Alles nach dem ersten "/" gilt als ID und erhielte eine eigene UUID. Im Kontext
#   der AETHER/DIMP-Pipeline (technische Identifier als IDs) kommen sie nicht vor.
################################################################################

# --- Imports ---
import argparse
import csv
import json
from pathlib import Path
import uuid
import sys
import pandas as pd

# --- KONFIGURATION (Kommandozeilenargumente) ---
parser = argparse.ArgumentParser(
    description="Re-Pseudonymisierung von CSV-Dateien: ersetzt IDs und FHIR-Referenzen durch UUIDs."
)
parser.add_argument("--input-dir", type=Path, required=True,
                    help="Eingabeordner mit den CSV-Dateien (wird inkl. Unterordnern durchsucht)")
parser.add_argument("--output-dir", type=Path, required=True,
                    help="Ausgabeordner für die pseudonymisierten CSV-Dateien (Ordnerstruktur wie im Eingabeordner)")
parser.add_argument("--mapping-dir", type=Path, required=True,
                    help="Ordner für die ID-Zuordnungen (Original-ID -> UUID); nicht mitliefern!")
parser.add_argument("--separator", default=None,
                    help="CSV-Trennzeichen für alle Dateien; ohne Angabe wird es je Datei automatisch erkannt")
args = parser.parse_args()

input_dir = args.input_dir
output_dir = args.output_dir
mapping_dir = args.mapping_dir

# Manuell angegebenes Trennzeichen prüfen ("\t" wird als Tabulator verstanden)
manual_separator = args.separator
if manual_separator == "\\t":
    manual_separator = "\t"
if manual_separator is not None and len(manual_separator) != 1:
    parser.error("--separator muss genau ein Zeichen sein (für Tabulator: \"\\t\")")

# Plausibilitätsprüfungen der Ordner
if not input_dir.is_dir():
    parser.error(f"Eingabeordner existiert nicht: {input_dir}")
_in, _out, _map = input_dir.resolve(), output_dir.resolve(), mapping_dir.resolve()
# Ausgabe- und Zuordnungsordner dürfen nicht im Eingabeordner liegen, sonst würden
# sie bei einem erneuten Lauf selbst als Eingabe eingelesen.
if _out.is_relative_to(_in) or _map.is_relative_to(_in):
    parser.error("Ausgabe- und Zuordnungsordner dürfen nicht im Eingabeordner liegen")
# Die ID-Zuordnungen dürfen nicht in den auszuliefernden Daten landen.
if _map.is_relative_to(_out) or _out.is_relative_to(_map):
    parser.error("Ausgabe- und Zuordnungsordner dürfen sich nicht überschneiden")

# Zielordner anlegen (falls noch nicht vorhanden)
output_dir.mkdir(parents=True, exist_ok=True)
mapping_dir.mkdir(parents=True, exist_ok=True)

# Mögliche CSV-Trennzeichen für die automatische Erkennung
CANDIDATE_SEPARATORS = (",", ";", "\t", "|")
# Anzahl der Zeilen, die für die Erkennung geprüft werden
SAMPLE_ROWS = 20


def detect_separator(file):
    """Erkennt das Trennzeichen einer CSV-Datei, ohne zu raten.

    Ein Kandidat gilt als passend, wenn die ersten SAMPLE_ROWS nicht-leeren Zeilen
    mit diesem Trennzeichen (unter Beachtung von Anführungszeichen) in allen Zeilen
    dieselbe Spaltenzahl > 1 ergeben.
    - Genau ein passender Kandidat: wird verwendet.
    - Kein Trennzeichen in der Kopfzeile (Datei mit nur einer Spalte): ",".
    - Sonst (mehrdeutig oder nicht erkennbar): ValueError. Die Datei wird dann
      übersprungen; mit --separator kann das Trennzeichen vorgegeben werden.
    """
    # encoding "utf-8-sig" entfernt ein eventuelles BOM am Dateianfang
    with open(file, newline="", encoding="utf-8-sig") as f:
        first_line = f.readline()
    if not first_line.strip():
        raise ValueError("Trennzeichen nicht erkennbar: Datei ist leer oder beginnt mit einer Leerzeile")
    if not any(sep in first_line for sep in CANDIDATE_SEPARATORS):
        return ","

    matching = []
    for sep in CANDIDATE_SEPARATORS:
        column_counts = []
        with open(file, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f, delimiter=sep):
                if not row:
                    continue  # Leerzeilen ignorieren (pandas überspringt sie ebenfalls)
                column_counts.append(len(row))
                if len(column_counts) >= SAMPLE_ROWS:
                    break
        if column_counts and column_counts[0] > 1 and len(set(column_counts)) == 1:
            matching.append(sep)

    if len(matching) == 1:
        return matching[0]
    if not matching:
        raise ValueError("Trennzeichen nicht erkennbar (keine konsistente Spaltenzahl); bitte --separator angeben")
    raise ValueError(f"Trennzeichen nicht eindeutig (passend: {matching!r}); bitte --separator angeben")


# Präfixe zur Erkennung von FHIR-Referenzen (Form "<Ressourcentyp>/<id>").
# Enthält alle 146 Ressourcentypen von FHIR R4.0.1; Abgleich am 15.10.2026 mit
# der offiziellen Liste (https://hl7.org/fhir/R4/resourcelist.html).
# Es ist ein Tupel, weil str.startswith() mehrere Präfixe nur als Tupel prüfen kann.
fhir_prefixes = (
"Account/", "ActivityDefinition/", "AdverseEvent/", "AllergyIntolerance/", "Appointment/",
"AppointmentResponse/", "AuditEvent/", "Basic/", "Binary/", "BiologicallyDerivedProduct/",
"BodyStructure/", "Bundle/", "CarePlan/", "CareTeam/", "ChargeItem/", "ChargeItemDefinition/",
"Claim/", "ClaimResponse/", "ClinicalImpression/", "Communication/", "CommunicationRequest/",
"CompartmentDefinition/", "Composition/", "ConceptMap/", "Condition/", "Consent/",
"Coverage/", "CoverageEligibilityRequest/", "CoverageEligibilityResponse/", "Device/",
"DeviceDefinition/", "DeviceMetric/", "DeviceRequest/", "DeviceUseStatement/", "DiagnosticReport/",
"DocumentManifest/", "DocumentReference/", "Endpoint/",
"Encounter/", "EnrollmentRequest/", "EnrollmentResponse/", "EpisodeOfCare/", "Evidence/",
"EvidenceVariable/", "EffectEvidenceSynthesis/",
"EventDefinition/", "ExampleScenario/", "ExplanationOfBenefit/",
"FamilyMemberHistory/", "Flag/", "Goal/", "GraphDefinition/", "Group/", "GuidanceResponse/",
"HealthcareService/", "ImplementationGuide/", "ImagingStudy/", "Immunization/",
"ImmunizationEvaluation/", "ImmunizationRecommendation/", "InsurancePlan/", "Invoice/",
"List/", "Location/", "Library/", "MedicinalProduct/",
"MedicinalProductAuthorization/", "MedicinalProductContraindication/", "MedicinalProductIndication/",
"MedicinalProductIngredient/", "MedicinalProductInteraction/", "MedicinalProductManufactured/",
"MedicinalProductPackaged/", "MedicinalProductPharmaceutical/", "MedicinalProductUndesirableEffect/",
"Media/", "Medication/", "MedicationAdministration/", "MedicationDispense/", "MedicationKnowledge/",
"MedicationRequest/", "MedicationStatement/", "MessageDefinition/", "MessageHeader/",
"MolecularSequence/", "Measure/", "MeasureReport/", "NamingSystem/", "NutritionOrder/",
"Observation/", "ObservationDefinition/", "OperationDefinition/", "Organization/",
"OrganizationAffiliation/", "Patient/", "Parameters/", "PaymentNotice/", "PaymentReconciliation/",
"Person/", "PlanDefinition/", "Practitioner/", "PractitionerRole/", "Procedure/",
"Provenance/", "Questionnaire/", "QuestionnaireResponse/", "RelatedPerson/", "ResearchDefinition/",
"ResearchElementDefinition/", "ResearchStudy/", "ResearchSubject/", "RequestGroup/",
"RiskAssessment/", "RiskEvidenceSynthesis/", "Schedule/", "SearchParameter/", "ServiceRequest/",
"Slot/", "Specimen/", "SpecimenDefinition/", "Subscription/", "Substance/",
"SubstanceNucleicAcid/", "SubstancePolymer/", "SubstanceProtein/", "SubstanceReferenceInformation/",
"SubstanceSpecification/", "SubstanceSourceMaterial/", "SupplyDelivery/", "SupplyRequest/",
"StructureDefinition/", "StructureMap/", "Task/", "TerminologyCapabilities/", "TestReport/",
"TestScript/", "VerificationResult/", "VisionPrescription/", "ValueSet/",
"CapabilityStatement/", "CatalogEntry/", "CodeSystem/", "Contract/", "DetectedIssue/",
"Linkage/", "OperationOutcome/"
)

# Alle Ordner (rekursiv) ermitteln, die mindestens eine CSV-Datei enthalten.
# Das Set verhindert, dass ein Ordner mehrfach verarbeitet wird.
folders_with_csv = set(file.parent for file in input_dir.rglob("*.csv"))

# Übersprungene Dateien (Fehler beim Einlesen oder Verarbeiten) als (Datei, Fehler)
skipped_files = []

# Jeder Ordner wird einzeln verarbeitet, mit eigener ID-Zuordnung je Ordner
for folder in folders_with_csv:
    # Zuordnung für den aktuellen Ordner: { Original-ID: UUID }
    # Sie wird beim Durchlaufen der Dateien laufend erweitert: Eine ID bekommt
    # ihre UUID beim ersten Auftreten (als "id" oder als Referenz), spätere
    # Vorkommen derselben ID nutzen dieselbe UUID.
    id_map = {}

    # Alle CSV-Dateien direkt in diesem Ordner (ohne Unterordner)
    csv_files_this_folder = list(folder.glob("*.csv"))

    for file in csv_files_this_folder:
        try:
            # Trennzeichen dieser Datei: manuell vorgegeben oder automatisch erkannt.
            # Bei nicht erkennbarem Trennzeichen wird ein ValueError ausgelöst und die
            # Datei unten im except-Block gemeldet und übersprungen.
            sep = manual_separator if manual_separator is not None else detect_separator(file)

            # Alle Spalten als Text einlesen (dtype=str), damit IDs und Werte
            # nicht in Zahlen o.ä. umgewandelt werden (z.B. führende Nullen).
            # keep_default_na=False verhindert, dass Texte wie "NA" oder "null"
            # als fehlende Werte gelten und im Ergebnis leer wären; nur wirklich
            # leere Felder (na_values=[""]) gelten weiterhin als leer (NaN).
            # pandas liest standardmäßig strikt als UTF-8: Bei ungültigen Zeichen wird ein
            # Fehler ausgelöst und die Datei unten gemeldet und übersprungen.
            df = pd.read_csv(file, sep=sep, dtype=str, keep_default_na=False, na_values=[""])

            # Kopie des DataFrames für die pseudonymisierten Daten; das Original bleibt unverändert
            df_pseudo = df.copy()

            # Jede Spalte einzeln durchgehen und IDs bzw. Referenzen ersetzen
            for col in df.columns:
                # Spalte als Liste, da die Iteration darüber schneller ist
                # als zeilenweise Zugriffe auf den DataFrame
                col_values = df[col].tolist()
                new_values = []

                for val in col_values:
                    # Leere Werte (NaN) unverändert übernehmen
                    if pd.isna(val):
                        new_values.append(val)
                        continue

                    # Wert als Text, ohne führende/nachfolgende Leerzeichen
                    val_str = str(val).strip()

                    # Fall A: Spalte "id" (eigene ID der Ressource)
                    if col == "id":
                        # ID ist bereits bekannt, z.B. weil sie zuvor als Referenz
                        # aufgetreten ist: vorhandene UUID wiederverwenden
                        if val_str in id_map:
                            new_values.append(id_map[val_str])
                        else:
                            # ID ist neu: UUID erzeugen, in der Zuordnung merken und einsetzen
                            new_uuid = str(uuid.uuid4())
                            id_map[val_str] = new_uuid
                            new_values.append(new_uuid)

                    # Fall B: FHIR-Referenz, z.B. "Patient/123"
                    # (Wert beginnt mit einem der Präfixe aus fhir_prefixes)
                    elif val_str.startswith(fhir_prefixes):
                        # Am ersten "/" trennen: "Patient/123" -> ["Patient", "123"]
                        prefix, original_id = val_str.split("/", 1)

                        # ID in diesem Ordner noch nicht bekannt: neue UUID erzeugen
                        if original_id not in id_map:
                            id_map[original_id] = str(uuid.uuid4())

                        # Ressourcentyp behalten, ID ersetzen: "Patient/<neue UUID>"
                        new_values.append(f"{prefix}/{id_map[original_id]}")

                    # Fall C: sonstiger Wert (keine ID, keine Referenz): unverändert übernehmen
                    else:
                        new_values.append(val)

                # Spalte im pseudonymisierten DataFrame durch die neuen Werte ersetzen
                df_pseudo[col] = new_values

            # Zielordner mit derselben Unterordnerstruktur wie im Eingabeordner anlegen
            target_output_folder = output_dir / folder.relative_to(input_dir)
            target_output_folder.mkdir(parents=True, exist_ok=True)

            # Pseudonymisierte Datei unter demselben Dateinamen und mit demselben Trennzeichen speichern
            df_pseudo.to_csv(target_output_folder / file.name, sep=sep, index=False)
            print(f"Datei pseudonymisiert und gespeichert: {target_output_folder / file.name} (Trennzeichen: {sep!r})")

        except Exception as e:
            # Fehler bei einer Datei brechen die Verarbeitung der übrigen Dateien nicht ab;
            # die Datei wird gemerkt und am Ende des Laufs gesammelt gemeldet
            skipped_files.append((file, e))
            print(f"Fehler bei der Pseudonymisierung von {file.name}: {e}", file=sys.stderr)

    # Zuordnung dieses Ordners als JSON speichern, im mapping_dir mit derselben
    # Ordnerstruktur wie im Eingabeordner (nur falls Einträge vorhanden sind)
    if id_map:
        # Zielpfad im mapping_dir nachbilden
        target_folder = mapping_dir / folder.relative_to(input_dir)

        # parents=True legt auch Zwischenordner an, exist_ok=True verhindert Fehler bei vorhandenem Ordner
        target_folder.mkdir(parents=True, exist_ok=True)

        mapping_file = target_folder / "id_pseudonyms.json"

        # ensure_ascii=False erhält Sonderzeichen; indent=4 macht die Datei lesbar
        with open(mapping_file, "w", encoding="utf-8") as f:
            json.dump(id_map, f, indent=4, ensure_ascii=False)

        print(f"Zuordnungsdatei erstellt für Ordner: {folder.relative_to(input_dir)}")
        print(f"-> Gespeichert unter: {mapping_file}\n")

# Warnung am Ende, falls Dateien nicht verarbeitet werden konnten
if skipped_files:
    print("\n" + "!" * 80, file=sys.stderr)
    print(f"WARNUNG: {len(skipped_files)} Datei(en) wurden NICHT pseudonymisiert und fehlen im Ausgabeordner:", file=sys.stderr)
    for skipped_file, error in skipped_files:
        print(f"  - {skipped_file}: {error}", file=sys.stderr)
    print("Die Ausgabe ist unvollständig. Bitte Ursache beheben und das Skript erneut ausführen.", file=sys.stderr)
    print("!" * 80, file=sys.stderr)
    sys.exit(1)