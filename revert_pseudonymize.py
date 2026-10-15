################################################################################
# revert_pseudonymize.py
#
# Gegenstück zu transfer_pseudonymize.py: Skript zur Rückführung
# (De-Pseudonymisierung) von CSV-Dateien, die mit transfer_pseudonymize.py
# pseudonymisiert wurden.
#
# Das Skript ersetzt die UUIDs in der Spalte `id` und in Referenzen der Form
# `Ressourcentyp/<uuid>` anhand der gespeicherten ID-Zuordnungen
# (id_pseudonyms.json) wieder durch die Original-IDs. Die Ergebnisse werden in
# einen Ausgabeordner geschrieben.
#
# Ablauf:
# 1. Einlesen der pseudonymisierten CSV-Dateien
# 2. Alle ID-Zuordnungen (id_pseudonyms.json) aus den Zuordnungsordnern laden,
#    zusammenführen und umkehren (UUID -> Original-ID)
# 3. Ersetzen der UUIDs durch die Original-IDs, und zwar
#    - in der Spalte "id" und
#    - in allen Spalten, deren Wert wie eine FHIR-Referenz aussieht,
#      z.B. "Patient/<UUID>" -> "Patient/123"
# 4. Schreiben der CSV-Dateien mit den Original-IDs
#
# Aufruf (Python >= 3.9, benötigt pandas):
#   python revert_pseudonymize.py --input-dir <Ordner mit pseudonymisierten Daten> \
#          --output-dir <Ausgabeordner> --mapping-dir <Zuordnungsordner> [<weitere> ...]
# Optional: --separator <Zeichen>, um die automatische Erkennung des CSV-Trennzeichens
# zu überschreiben (für Tabulator: --separator "\t").
#
# Zuordnungen: In den Zuordnungsordnern werden rekursiv alle Dateien
# id_pseudonyms.json gesucht und zu einer gemeinsamen Zuordnung zusammengeführt.
# Daten- und Zuordnungsordner müssen sich daher nicht einander zuordnen lassen
# (die Ordnernamen dürfen sich unterscheiden, z.B. bei umbenannten Standortordnern).
# Das ist möglich, weil die UUIDs zufällig (UUID v4) und damit praktisch eindeutig
# sind. Verweist dieselbe UUID auf verschiedene Original-IDs, bricht das Skript ab.
#
# Dateien, die nicht verarbeitet werden können (z.B. falsche Kodierung oder nicht
# erkennbares Trennzeichen), werden übersprungen. Werte, die nicht in den
# Zuordnungen stehen, bleiben unverändert. In beiden Fällen gibt das Skript am
# Ende eine Warnung aus.
#
# Rückgabewert (Exit-Code):
#   0 = alle Dateien wurden vollständig zurückgeführt
#   1 = mindestens eine Datei wurde übersprungen oder es gab Werte ohne Zuordnung
#       (Ausgabe unvollständig); ebenso bei nicht lesbaren oder widersprüchlichen
#       Zuordnungen (dann Abbruch vor der Verarbeitung)
#   2 = ungültige Aufrufparameter (z.B. fehlendes Argument, ungültiger Ordner,
#       keine id_pseudonyms.json in den Zuordnungsordnern)
#
# Trennzeichen: Es wird für jede CSV-Datei einzeln erkannt (",", ";", Tabulator
# oder "|") und beim Schreiben der Datei beibehalten.
#
# Wichtig:
# - Die Zuordnungen müssen aus demselben Lauf von transfer_pseudonymize.py stammen
#   wie die pseudonymisierten Daten, da bei jedem Lauf neue UUIDs entstehen.
#   Zuordnungen weiterer Läufe oder Standorte in den Zuordnungsordnern stören nicht.
# - Versionierte Referenzen (z. B. "Patient/<UUID>/_history/2") werden nicht
#   unterstützt (siehe transfer_pseudonymize.py).
# - Bei erneuter Ausführung werden die Ausgabedateien überschrieben. Dateien im
#   Ausgabeordner, zu denen es keine Eingabedatei mehr gibt, werden nicht gelöscht.
# - Der Ausgabeordner enthält wieder die Original-IDs und ist entsprechend zu schützen.
################################################################################

# --- Imports ---
import argparse
import csv
import json
from pathlib import Path
import sys
import pandas as pd

# --- KONFIGURATION (Kommandozeilenargumente) ---
parser = argparse.ArgumentParser(
    description="Rückführung pseudonymisierter CSV-Dateien: ersetzt UUIDs durch die Original-IDs."
)
parser.add_argument("--input-dir", type=Path, required=True,
                    help="Eingabeordner mit den pseudonymisierten CSV-Dateien (wird inkl. Unterordnern durchsucht)")
parser.add_argument("--output-dir", type=Path, required=True,
                    help="Ausgabeordner für die zurückgeführten CSV-Dateien (Ordnerstruktur wie im Eingabeordner)")
parser.add_argument("--mapping-dir", type=Path, nargs="+", required=True,
                    help="Ein oder mehrere Ordner mit den ID-Zuordnungen (alle id_pseudonyms.json darin werden rekursiv geladen)")
parser.add_argument("--separator", default=None,
                    help="CSV-Trennzeichen für alle Dateien; ohne Angabe wird es je Datei automatisch erkannt")
args = parser.parse_args()

input_dir = args.input_dir
output_dir = args.output_dir
mapping_dirs = args.mapping_dir

# Manuell angegebenes Trennzeichen prüfen ("\t" wird als Tabulator verstanden)
manual_separator = args.separator
if manual_separator == "\\t":
    manual_separator = "\t"
if manual_separator is not None and len(manual_separator) != 1:
    parser.error("--separator muss genau ein Zeichen sein (für Tabulator: \"\\t\")")

# Plausibilitätsprüfungen der Ordner
if not input_dir.is_dir():
    parser.error(f"Eingabeordner existiert nicht: {input_dir}")
for mapping_dir in mapping_dirs:
    if not mapping_dir.is_dir():
        parser.error(f"Zuordnungsordner existiert nicht: {mapping_dir}")
_in, _out = input_dir.resolve(), output_dir.resolve()
# Der Ausgabeordner darf nicht im Eingabeordner liegen, sonst würden die Ergebnisse
# bei einem erneuten Lauf selbst als Eingabe eingelesen.
if _out.is_relative_to(_in):
    parser.error("Der Ausgabeordner darf nicht im Eingabeordner liegen")
# Die ID-Zuordnungen dürfen nicht in den Ergebnisdaten landen.
for mapping_dir in mapping_dirs:
    _map = mapping_dir.resolve()
    if _map.is_relative_to(_out) or _out.is_relative_to(_map):
        parser.error("Ausgabe- und Zuordnungsordner dürfen sich nicht überschneiden")

# Zielordner anlegen (falls noch nicht vorhanden)
output_dir.mkdir(parents=True, exist_ok=True)

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


def load_reverse_mapping():
    """Lädt alle id_pseudonyms.json aus den Zuordnungsordnern (rekursiv) und kehrt sie um.

    Ergebnis: {UUID: Original-ID} über alle gefundenen Zuordnungen. Die UUIDs sind
    zufällig (UUID v4) und damit praktisch eindeutig; eine UUID, die auf verschiedene
    Original-IDs verweist, gilt als Fehler (Abbruch).
    """
    mapping_files = sorted(f for d in mapping_dirs for f in d.rglob("id_pseudonyms.json"))
    if not mapping_files:
        parser.error("In den Zuordnungsordnern wurde keine id_pseudonyms.json gefunden")

    reverse_map = {}
    for mapping_file in mapping_files:
        try:
            with open(mapping_file, "r", encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception as e:
            sys.exit(f"Fehler: Zuordnung nicht lesbar: {mapping_file}: {e}")
        for original_id, uuid_value in mapping.items():
            if reverse_map.setdefault(uuid_value, original_id) != original_id:
                sys.exit(f"Fehler: UUID {uuid_value} verweist auf verschiedene Original-IDs "
                         f"(u.a. in {mapping_file})")

    print(f"{len(mapping_files)} Zuordnungsdatei(en) mit {len(reverse_map)} UUIDs geladen.")
    return reverse_map


def revert_value(val, col, reverse_map, unmapped):
    """Führt eine Zelle auf die Original-ID zurück.

    - Fall A: Spalte "id" -> UUID durch Original-ID ersetzen
    - Fall B: FHIR-Referenz (z.B. "Patient/<UUID>") -> UUID durch Original-ID ersetzen
    - Fall C: sonstiger Wert -> unverändert
    Werte ohne Eintrag in der Zuordnung bleiben unverändert und werden in der
    Menge `unmapped` vermerkt.
    """
    # Leere Werte (NaN) unverändert übernehmen
    if pd.isna(val):
        return val

    # Wert als Text, ohne führende/nachfolgende Leerzeichen
    val_str = str(val).strip()

    # Fall A: Spalte "id"
    if col == "id":
        if val_str in reverse_map:
            return reverse_map[val_str]
        unmapped.add(val_str)
        return val

    # Fall B: FHIR-Referenz (Wert beginnt mit einem der Präfixe aus fhir_prefixes)
    if val_str.startswith(fhir_prefixes):
        # Am ersten "/" trennen: "Patient/<UUID>" -> ["Patient", "<UUID>"]
        prefix, uuid_part = val_str.split("/", 1)
        if uuid_part in reverse_map:
            return f"{prefix}/{reverse_map[uuid_part]}"
        unmapped.add(val_str)
        return val

    # Fall C: sonstiger Wert (keine ID, keine Referenz): unverändert übernehmen
    return val


# Gemeinsame Zuordnung aller Standorte laden (UUID -> Original-ID)
reverse_map = load_reverse_mapping()

# Nicht verarbeitete Dateien als (Pfad, Grund)
skipped = []
# Dateien mit Werten ohne Zuordnung als (Datei, Anzahl, Beispiele)
files_with_unmapped = []

# Alle Ordner (rekursiv) ermitteln, die mindestens eine CSV-Datei enthalten
folders_with_csv = sorted(set(file.parent for file in input_dir.rglob("*.csv")))

for folder in folders_with_csv:
    relative_folder = folder.relative_to(input_dir)
    csv_files = list(folder.glob("*.csv"))

    for file in csv_files:
        try:
            # Trennzeichen dieser Datei: manuell vorgegeben oder automatisch erkannt
            sep = manual_separator if manual_separator is not None else detect_separator(file)

            # Alle Spalten als Text einlesen (dtype=str), damit IDs und Werte nicht
            # umgewandelt werden. keep_default_na=False verhindert, dass Texte wie "NA"
            # oder "null" als fehlende Werte gelten; nur leere Felder gelten als leer.
            # pandas liest standardmäßig strikt als UTF-8: Bei ungültigen Zeichen wird ein
            # Fehler ausgelöst und die Datei unten gemeldet und übersprungen.
            df = pd.read_csv(file, sep=sep, dtype=str, keep_default_na=False, na_values=[""])
            df_result = df.copy()

            # Werte ohne Eintrag in der Zuordnung (für diese Datei)
            unmapped = set()

            # Jede Spalte einzeln durchgehen und UUIDs bzw. Referenzen zurückführen
            for col in df.columns:
                df_result[col] = [revert_value(val, col, reverse_map, unmapped) for val in df[col].tolist()]

            # Zielordner mit derselben Unterordnerstruktur wie im Eingabeordner anlegen
            target_folder = output_dir / relative_folder
            target_folder.mkdir(parents=True, exist_ok=True)

            # Datei unter demselben Namen und mit demselben Trennzeichen speichern
            df_result.to_csv(target_folder / file.name, sep=sep, index=False)
            print(f"Datei gespeichert: {target_folder / file.name} (Trennzeichen: {sep!r})")

            if unmapped:
                files_with_unmapped.append((file, len(unmapped), sorted(unmapped)[:3]))
                print(f"Warnung: {len(unmapped)} Wert(e) in {file.name} nicht in der Zuordnung gefunden "
                      f"und unverändert übernommen.", file=sys.stderr)

        except Exception as e:
            # Fehler bei einer Datei brechen die Verarbeitung der übrigen Dateien nicht ab;
            # die Datei wird gemerkt und am Ende des Laufs gesammelt gemeldet
            skipped.append((file, e))
            print(f"Fehler bei der Verarbeitung von {file.name}: {e}", file=sys.stderr)

print("\nVerarbeitung abgeschlossen.")

# Warnung am Ende, falls die Rückführung nicht vollständig war
if skipped or files_with_unmapped:
    print("\n" + "!" * 80, file=sys.stderr)
    if skipped:
        print(f"WARNUNG: {len(skipped)} Datei(en) wurden NICHT zurückgeführt und fehlen im Ausgabeordner:", file=sys.stderr)
        for path, reason in skipped:
            print(f"  - {path}: {reason}", file=sys.stderr)
    if files_with_unmapped:
        print(f"WARNUNG: In {len(files_with_unmapped)} Datei(en) gibt es Werte ohne Zuordnung (unverändert übernommen):", file=sys.stderr)
        for path, count, examples in files_with_unmapped:
            print(f"  - {path}: {count} Wert(e), z.B. {examples}", file=sys.stderr)
    print("Die Ausgabe ist unvollständig. Bitte Ursache beheben (z.B. passende Zuordnung angeben) und das Skript erneut ausführen.", file=sys.stderr)
    print("!" * 80, file=sys.stderr)
    sys.exit(1)