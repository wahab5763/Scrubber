"""Local extraction and redaction. Never import the provider client here."""
import argparse
import csv
import io
import re
import unicodedata
from pathlib import Path

import pdfplumber
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from zipfile import ZipFile

LIMIT = 8_000_000
FORMATS = {".pdf", ".docx", ".csv", ".txt"}
# Country-specific lengths let the IBAN matcher stop at the right boundary.
IBAN_LENGTHS = {item[:2]: int(item[2:]) for item in (
    "AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BR29 BY28 CH21 CR22 CY28 CZ24 DE22 "
    "DK18 DO28 EE20 EG29 ES24 FI18 FO18 FR27 GB22 GE22 GI23 GL18 GR27 GT28 HR21 HU28 "
    "IE22 IL23 IQ23 IS26 IT27 JO30 KW30 KZ20 LB28 LC32 LI21 LT20 LU20 LV21 LY25 MC27 "
    "MD24 ME22 MK19 MR27 MT31 MU30 NL18 NO15 OM23 PK24 PL28 PS29 PT25 QA29 RO24 RS22 "
    "SA24 SC31 SD18 SE24 SI19 SK24 SM27 SO23 ST25 SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20"
).split()}
LABELS = {
    "ADDRESS": r"(?:(?:home|customer|billing|shipping|mailing|registered|primary|postal)\s+)?address|anschrift|adresse",
    "ACCOUNT_ID": r"(?:wallet\s+)?account\s*(?:number|no\.?|#|id)|a/c\s*(?:no\.?|#|number)|acct(?:\s*(?:no\.?|#|number))?|kontonummer|konto|account|acc",
    "NATIONAL_ID": r"(?:registered\s+)?cnic(?:\s*(?:no\.?|number|#))?|ssn(?:\s+on\s+file)?|sin|ni(?:no|\s+number)?|national (?:insurance|id|identity)(?: number)?|pan|aadhaar|aadhar|steuer[- ]?id|passport(?: number| no)?",
    "ROUTING": r"routing(?: number| no)?|sort(?: code)?|swift(?:/bic)?|bic|ifsc|micr|transit(?: number)?|institution(?: number)?|blz|bankleitzahl|bsb",
    "PHONE": r"(?:(?:registered|primary|alternate|contact)\s+)?(?:phone|mobile|tel\.?|telefon(?:nummer)?|cell)(?:\s*(?:no\.?|number|#))?",
    "HANDLE": r"(?:wallet|upi|payment)\s+(?:id|handle)|vpa",
    "CARD": r"(?:linked\s+)?(?:credit\s+|debit\s+)?card(?:\s*(?:number|no\.?|#))?",
    "EMAIL": r"(?:registered\s+)?e[- ]?mail(?: address)?",
    "REFERENCE": r"(?:(?:transaction|confirmation|trace|reference|consumer|customer|invoice|bill|meter|authorization|auth|mandate|agreement|mandatsreferenz|gl(?:ä|ae)ubiger)\s*[- ]?\s*(?:id|ids|number|no\.?|ref|reference|code|#)|(?:ach\s+)?trace|ref(?:erence)?|utr|rrn|invoice|kundennummer)",
    "IP": r"(?:login\s+)?ip(?: address)?(?:\s*\([^)]*\))?",
    "IBAN": r"iban",
}
TOKEN = r"[A-Za-z0-9][A-Za-z0-9_*.-]*"
MASKED = re.compile(r"(?i)(?:[x*•]{2,}[ -]?\d{4}|ending(?: in)?\s+\d{4})\b")
FIELD_NAMES = {kind: re.compile(rf"(?:{pattern})", re.I) for kind, pattern in LABELS.items()}


def iban_at(match):
    """Consume exactly the country length, including line-wrapped groups."""
    source = match.string
    start = match.start()
    size = IBAN_LENGTHS.get(source[start:start+2].upper())
    if size is None:
        return None
    end = start + 4
    count = 4
    while count < size:
        part = re.match(r"[ \t\n]*([A-Za-z0-9]+)", source[end:])
        if part is None or count + len(part[1]) > size:
            return None
        end += part.end()
        count += len(part[1])
    return end if not re.match(r"[A-Za-z0-9]", source[end:]) else None


def redact(text):
    """Replace recognized identifiers while retaining dates, names, merchants, and amounts."""
    # Normalize visually equivalent text before applying the identifier patterns.
    text = unicodedata.normalize("NFC", text)
    text = ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                   for c in text if unicodedata.category(c) != "Cf")
    text = re.sub(r"[^\S\n]", " ", text)
    # Remove labeled address fields before general identifier matching can split them.
    text = re.sub(rf"(?im)^[ \t]*(?P<label>{LABELS['ADDRESS']})[ \t]*[:=][ \t]*[^\n]*",
                  r"\g<label>: <ADDRESS>", text)

    # Handle long, structured identifiers first so later broad patterns do not truncate them.
    text = re.sub(r"(?im)^([ \t]*IBAN[ \t]*:[ \t]*)[A-Z]{2}\d{2}[A-Z0-9 ]*(?:\n[ \t]*\d{1,6}[ \t]*(?=\n|$))?", r"\1<IBAN>", text)
    text = re.sub(r"(?i)(\bsort code[ :]+)\d{2}-\d{2}-\d{2} *\(\d{6}\)", r"\1<ROUTING>", text)
    text = re.sub(r"(?i)\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2,}\b",
                  lambda m: "<REFERENCE>" if sum(c.isdigit() for c in m[0]) >= 6 else m[0], text)
    for match in reversed(list(re.finditer(r"(?i)\b[A-Z]{2}\d{2}(?=[A-Z0-9 \n])", text))):
        end = iban_at(match)
        if end and end <= len(text) and "<" not in text[match.start():end]:
            text = text[:match.start()] + "<IBAN>" + text[end:]

    # Redact common identifier forms even when the statement does not label them.
    text = re.sub(r"(?i)(?<![\w@])[\w.!#$%&'*+=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*",
                  lambda m: "<EMAIL>" if "." in m[0].split("@")[1] else "<HANDLE>", text)
    text = re.sub(r"(?<![\w@])@[\w-]{2,}(?:\.[\w-]+)*", "<HANDLE>", text)
    text = re.sub(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?![.\d])", "<IP>", text)
    text = re.sub(r"(?i)(?<!\w)(?:\d{5}-\d{7}-\d|\d{3}-\d{2}-\d{4}|\d{4}-\d{4}-\d{4}|[A-Z]{2}\d{6}[A-D]|[A-Z]{5}\d{4}[A-Z])(?!\w)", "<NATIONAL_ID>", text)
    text = re.sub(r"(?i)\b[A-Z]{4}0[A-Z0-9]{6}\b", "<ROUTING>", text)
    text = re.sub(r"(?<!\w)\d{4,}[ -]?[xX*•]{2,}[ -]?\d{2,}(?!\w)", "<CARD>", text)
    text = re.sub(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<REFERENCE>", text)
    text = re.sub(r"(?i)\b(?:\d{2,4}[ -]){3}\d{3,4}\b", "<CARD>", text)

    # Contextual rules use nearby field labels to distinguish IDs from legitimate numbers.
    for kind in ("IBAN", "ACCOUNT_ID", "NATIONAL_ID", "ROUTING", "PHONE", "HANDLE", "CARD", "REFERENCE", "IP"):
        label = LABELS[kind]
        if kind == "PHONE":
            value = r"\+?\(?\d[\d ()-]{4,}\d(?:[ \t]*/[ \t]*\+?\(?\d[\d ()-]{4,}\d)?"
        elif kind in {"ACCOUNT_ID", "NATIONAL_ID", "ROUTING"}:
            value = r"(?:\d{2,6}(?:[ -]\d{2,6})+|[A-Z]{2}[ ]\d{2}[ ]\d{2}[ ]\d{2}[ ][A-D]|" + TOKEN + ")"
        elif kind == "CARD":
            value = r"(?:(?:[x*•]{2,})[ -]?\d{4}|(?:\d[ -]?){5,19}\d|" + TOKEN + ")"
        else:
            value = TOKEN
        pattern = rf"(?i)\b({label})([ \t]*[:=#][ \t]*|[ \t]+)({value})"

        def replace(match):
            raw = match[3]
            explicit = any(c in match[2] for c in ":=#")
            if MASKED.fullmatch(raw):
                return match[0]
            if re.match(r"^[.,]\d", match.string[match.end():]) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return match[0]
            has_digit = any(c.isdigit() for c in raw)
            if not has_digit and not (explicit and kind in {"ACCOUNT_ID", "HANDLE", "ROUTING", "REFERENCE"}):
                if kind != "ROUTING" or match[1].lower() not in {"swift", "swift/bic", "bic"} or len(raw) not in {8, 11}:
                    return match[0]
            return f"{match[1]}{match[2]}<{kind}>"

        text = re.sub(pattern, replace, text)

    # Broad fallbacks catch unlabeled phone/reference/ID patterns missed by contextual rules.
    text = re.sub(r"(?<![\w.])\+\d{1,3}[ .-]?(?:\(?\d{1,5}\)?[ .-])+\d{3,10}(?![\d.,])", "<PHONE>", text)
    text = re.sub(r"(?<!\w)(?:\(\d{2,5}\)[ -]?\d{3,8}(?:-\d{3,4})?|\d{2,5}[- ]\d{5,9}|\d{3}-\d{3}-\d{4})(?!\w)", "<PHONE>", text)
    text = re.sub(r"(?<![\w.,])\d{2,7}(?:-\d{2,9}){2,}(?![\w.,])",
                  lambda m: m[0] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", m[0]) else "<REFERENCE>", text)
    text = re.sub(r"(?<![\w.,])\d{7,}(?!\w|[.,]\d)", "<NUMBER_ID>", text)
    text = re.sub(r"\b(?=[A-Z0-9-]{8,}\b)(?=[A-Z0-9-]*[A-Z])(?=(?:[A-Z-]*\d){5})[A-Z0-9-]+\b", "<REFERENCE>", text)
    return text


def field_kind(label):
    """Map a column or metadata label to the sensitive-value type it identifies."""
    return next((kind for kind, pattern in FIELD_NAMES.items() if pattern.fullmatch(label.strip(" :#"))), None)


def csv_rows(rows):
    """Preserve cell boundaries and process both key/value and tabular exports."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    columns = {}
    for row in rows:
        # A transaction header establishes which columns contain sensitive values.
        if len(row) > 1 and any(cell.strip().lower() in {"date", "timestamp", "transaction date"} for cell in row):
            columns = {i: field_kind(cell) for i, cell in enumerate(row)}
            writer.writerow([redact(cell) for cell in row])
            continue
        key_kind = field_kind(row[0]) if len(row) > 1 else None
        clean = []
        for i, value in enumerate(row):
            kind = (key_kind if i == 1 else None) or columns.get(i)
            if kind and value.strip() and not MASKED.fullmatch(value.strip()):
                clean.append(f"<{kind}>")
            else:
                clean.append(redact(value))
        writer.writerow(clean)
    return output.getvalue().rstrip("\n")


def extract(filename, payload):
    """Validate and extract one supported file entirely in-process."""
    suffix = Path(filename).suffix.lower()
    if not payload or len(payload) > LIMIT or suffix not in FORMATS:
        raise ValueError("Expected a nonempty PDF, DOCX, CSV or TXT under 8 MB.")
    source = io.BytesIO(payload)
    # Each parser preserves the structure needed by later redaction and ledger parsing.
    if suffix == ".pdf":
        with pdfplumber.open(source) as pdf:
            if not pdf.pages or len(pdf.pages) > 100:
                raise ValueError("Expected a PDF of 1–100 pages.")
            pages = [page.extract_text(layout=True).strip() for page in pdf.pages]
            if not all(pages):
                raise ValueError("A page lacks extractable text. OCR is required first.")
            clean = redact("\n".join(pages))
    elif suffix == ".docx":
        with ZipFile(source) as archive:
            if sum(member.file_size for member in archive.infolist()) > 24_000_000:
                raise ValueError("DOCX expands beyond 24 MB.")
        document = Document(source)
        parts = []
        for item in document.element.body:
            if item.tag.endswith("}p"):
                parts.append(Paragraph(item, document).text)
            elif item.tag.endswith("}tbl"):
                for row in Table(item, document).rows:
                    cells = [c.text for c in row.cells]
                    if len(cells) == 2 and (kind := field_kind(cells[0])) and cells[1].strip():
                        if not MASKED.fullmatch(cells[1].strip()):
                            cells[1] = f"<{kind}>"
                    parts.append(" | ".join(cells))
        clean = redact("\n".join(parts))
    elif suffix == ".csv":
        clean = csv_rows(csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True))
    else:
        clean = redact(payload.decode("utf-8-sig"))
    if not clean.strip() or len(clean) > 1_000_000:
        raise ValueError("Expected 1–1,000,000 extracted characters.")
    return clean


def safe_name(name):
    """Keep only a redacted basename so client paths and identifying filenames are not stored."""
    return redact(re.sub(r"[\x00-\x1f\x7f]", "_", name.replace("\\", "/").rsplit("/", 1)[-1]))[:180]


def combine(files):
    """Join scrubbed files into one readable string with an explicit header per file."""
    return "\n\n".join(f"---- File: {name} ----\n{body}" for name, body in files)


def scrub_folder(folder):
    """Recursively scrub a folder in stable path order for the command-line workflow."""
    paths = sorted(path for path in Path(folder).rglob("*") if path.is_file())
    if not paths:
        raise ValueError("The input folder is empty.")
    return combine((safe_name(path.name), extract(path.name, path.read_bytes())) for path in paths)


if __name__ == "__main__":
    # Keep folder and output path configurable rather than tying the CLI to sample filenames.
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()
    Path(args.output).write_text(scrub_folder(args.folder) + "\n", encoding="utf-8")
