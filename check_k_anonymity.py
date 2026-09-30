import csv
from collections import Counter
import configargparse


parser = configargparse.ArgumentParser(
    description="Check k-anonymity",
    add_help=False
)

parser.add_argument("-k", "--kay")  # The k in k-anonymity. Default is 5.
parser.add_argument("-i", "--input-file")
parser.add_argument("-q", "--quasi-identifiers")  # column names, comma separated
parser.add_argument("-e", "--encoding")  # Default is utf-8 (with our without BOM)
parser.add_argument("-d", "--delimiter")  # Default is ';'

args, unknown = parser.parse_known_args()

KAY = getattr(args, "kay")
if KAY:
    KAY = int(KAY)
if not KAY:
    KAY = 5
INPUT_FILE = getattr(args, "input_file")
QUASI_IDENTIFIERS = [qi.strip() for qi in getattr(args, "quasi_identifiers").split(",")]
ENCODING = getattr(args, "encoding")
if not ENCODING:
    ENCODING = "utf-8-sig"
DELIMITER = getattr(args, "delimiter")
if not DELIMITER:
    DELIMITER = ";"


with open(INPUT_FILE, newline="", encoding=ENCODING) as f:
    reader = csv.DictReader(f, delimiter=DELIMITER)
    counts = Counter(
        tuple(row[col] for col in QUASI_IDENTIFIERS)
        for row in reader
    )

violations = {k_: v for k_, v in counts.items() if v < KAY}

if not violations:
    print(f"✅ Dataset satisfies {KAY}-anonymity")
else:
    print("❌ Violations:")
    for qi_vals, count in violations.items():
        print(qi_vals, count)
