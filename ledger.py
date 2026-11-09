"""Conservative ledger projection; transaction rows are (ref, date, description, Decimal, currency)."""
import csv
import re
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date as Date, datetime
from decimal import Decimal

DATE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?|"
    r"\d{1,2}(?:[- ]+[A-Za-z]{3,9}[- ]+|[./]\d{1,2}[./])\d{4})\s*"
)
# Currency symbols are parsed separately from codes because "$" is ambiguous across countries.
CURRENCY = r"USD|GBP|CAD|EUR|INR|PKR|AUD|AED|SAR|JPY|CHF|NZD|SGD"
MONEY = re.compile(
    rf"(?<![\w.,])(?P<sign>[+-])?\s*(?P<prefix>{CURRENCY}|C\$|\$|£|€|₹)?\s*"
    rf"(?P<amount>\d[\d,.]*[.,]\d{{2}})\s*(?P<suffix>CR|DR|{CURRENCY})?(?![\w.,])",
    re.I,
)
SYMBOLS = {"£": "GBP", "€": "EUR", "₹": "INR", "C$": "CAD"}
COUNTRY_CURRENCY = (
    (re.compile(r"\b(?:usa|u\.s\.a\.?|united states|american)\b", re.I), "USD"),
    (re.compile(r"\b(?:canada|canadian)\b", re.I), "CAD"),
    (re.compile(r"\b(?:australia|australian)\b", re.I), "AUD"),
    (re.compile(r"\b(?:new zealand|new zealand)\b", re.I), "NZD"),
    (re.compile(r"\b(?:singapore|singaporean)\b", re.I), "SGD"),
    (re.compile(r"\b(?:pakistan|pakistani)\b", re.I), "PKR"),
    (re.compile(r"\b(?:india|indian)\b", re.I), "INR"),
    (re.compile(r"\b(?:united kingdom|britain|england|uk)\b", re.I), "GBP"),
    (re.compile(r"\b(?:germany|deutschland|german)\b", re.I), "EUR"),
)


def money(match, fallback):
    """Convert a matched amount to Decimal and use explicit currency before context."""
    digits = match["amount"]
    decimal = digits[-3]
    value = Decimal(digits.replace("," if decimal == "." else ".", "").replace(decimal, "."))
    suffix = (match["suffix"] or "").upper()
    prefix = (match["prefix"] or "").upper()
    currency = suffix if suffix not in {"", "CR", "DR"} else SYMBOLS.get(prefix, prefix or fallback)
    if currency == "$":
        currency = fallback
    return (-value if match["sign"] == "-" or suffix == "DR" else value), currency


def transactions(ordinal, name, body):
    """Parse supported date-led statement rows and retain stable source-line references."""
    suffix = name.lower().rsplit(".", 1)[-1]
    currency_hint = f"Unspecified (file {ordinal})"
    date_order = None
    for line_no, line in enumerate(body.splitlines(), 1):
        # Statement headers can establish currency and date order for following rows.
        country_currency = next(
            (currency for pattern, currency in COUNTRY_CURRENCY if pattern.search(line)),
            None,
        )
        if country_currency:
            currency_hint = country_currency
        currency_code = re.search(r"\b(?:USD|GBP|CAD|EUR|INR|PKR|AUD|AED|SAR|JPY|CHF|NZD|SGD)\b", line, re.I)
        if currency_code:
            currency_hint = currency_code.group().upper()
        if re.search(r"\b(?:usa|u\.s\.a\.?|united states|american)\b", line, re.I):
            date_order = "month_first"
        elif re.search(r"\b(?:united kingdom|britain|england|germany|deutschland|pakistan)\b", line, re.I):
            date_order = "day_first"
        if not (date := DATE.match(line)):
            continue
        rest = line[date.end():].strip()
        # Each supported extraction format has a slightly different row shape.
        if suffix in {"csv", "txt"} and rest.startswith(","):
            cells = next(csv.reader([rest[1:]]))
            if len(cells) >= 6:
                description, kind, status, amount = cells[:4]
                if re.search(r"pending|cancel|fail|declin", status, re.I):
                    continue
                description = f"{description.strip()} · {kind.strip()}"
            elif len(cells) >= 4:
                kind, description, amount = cells[:3]
                description = f"{description.strip()} · {kind.strip()}"
            else:
                continue
            match = MONEY.fullmatch(amount.strip())
        elif suffix == "docx" and rest.startswith("|"):
            cells = rest[1:].split("|")
            if len(cells) != 3:
                continue
            description, amount, _ = cells
            match = MONEY.fullmatch(amount.strip())
        else:
            values = list(MONEY.finditer(rest))
            if len(values) < 2:
                continue
            match = values[-2]
            description = rest[:match.start()]
        if match:
            value, currency = money(match, currency_hint)
            date_text = date[1]
            if date_order and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", date_text):
                first, second, year = (int(part) for part in date_text.split("/"))
                month, day = (first, second) if date_order == "month_first" else (second, first)
                try:
                    date_text = Date(year, month, day).isoformat()
                except ValueError:
                    pass
            yield f"F{ordinal}:{line_no}", date_text, description.strip(), value, currency


CATEGORIES = {
    "Groceries": r"\bgrocer(?:y|ies)\b|supermarket|tesco|aldi|lidl|whole foods|loblaws|im?tiaz super market|grocery",
    "Dining and takeaway": r"foodpanda|restaurant|cafe|coffee|pizza|burger|dining|starbucks|tim hortons|chai wala|takeaway|take out",
    "Subscriptions": r"netflix|spotify|subscription|prime video|apple\.com|youtube|github",
    "Transport": r"uber|careem|fuel|petrol|parking|transit|transport|presto fare",
    "Utilities": r"utility|utilities|electric|gas bill|internet|mobile top|water bill|energy",
    "Transfers and cash": r"transfer|withdrawal|atm|e-transfer|remittance|p2p|zelle|venmo|raast|ibft",
    "Fees": r"\bfee\b|charge|charges",
    "Tax": r"\btax\b|\btaxes\b|\bvat\b|\bgst\b|\bwht\b|withholding",
}
NON_SPENDING_ADJUSTMENT = re.compile(r"\b(?:refund|refunds|reversal|reversed|chargeback)\b", re.I)
CATEGORY_NAMES = (*CATEGORIES, "Unknown")
CATEGORY_ALIASES = {
    "groceries": "Groceries",
    "grocery": "Groceries",
    "food and groceries": "Groceries",
    "dining": "Dining and takeaway",
    "dining and takeaway": "Dining and takeaway",
    "takeaway": "Dining and takeaway",
    "restaurants": "Dining and takeaway",
    "subscriptions": "Subscriptions",
    "transport": "Transport",
    "utilities": "Utilities",
    "transfers": "Transfers and cash",
    "transfers and cash": "Transfers and cash",
    "fees": "Fees",
    "tax": "Tax",
    "unknown": "Unknown",
}
MONTHS = {
    alias.casefold(): index
    for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"),
        1,
    )
    for alias in (name, name[:3])
}
TIPS = {
    "review": "Review the largest outflows before setting a spending target.",
    "subscriptions": "Check whether each recurring subscription is still used.",
    "separate": "Track transfers and cash separately from day-to-day purchases.",
    "food": "Compare food purchases over time before setting a grocery or dining limit.",
}


def categorize(description):
    """Assign the first matching transparent category, otherwise keep it Unknown."""
    return next(
        (label for label, pattern in CATEGORIES.items() if re.search(pattern, description, re.I)),
        "Unknown",
    )


def is_spending_outflow(row):
    """Count negative transactions as spending except identifiable reversals/refunds."""
    return row[3] < 0 and not NON_SPENDING_ADJUSTMENT.search(row[2])


def transaction_date(value):
    """Normalize supported dates; return None instead of guessing ambiguous numeric dates."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", value):
        try:
            return Date.fromisoformat(value[:10])
        except ValueError:
            return None
    for pattern in ("%d %b %Y", "%d %B %Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b, %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
        first, second, year = (int(part) for part in value.split("/"))
        if first > 12:
            first, second = second, first
        elif second <= 12:
            return None
        try:
            return Date(year, first, second)
        except ValueError:
            return None
    return None


def transactions_from_documents(documents):
    """Project ordered stored documents into one list of source-cited transaction rows."""
    rows = [row for ordinal, name, body in documents for row in transactions(ordinal, name, body)]
    return rows


def overview(rows):
    """Build per-currency totals and largest-outflow facts for the analysis stage."""
    outflows = [row for row in rows if is_spending_outflow(row)]
    groups = defaultdict(list)
    facts = []
    for row in rows:
        _, _, desc, amount, currency = row
        if not is_spending_outflow(row):
            continue
        category = categorize(desc)
        groups[currency, "All outflows"].append(row)
        groups[currency, category].append(row)
    for (currency, label), entries in sorted(groups.items()):
        total = sum(-entry[3] for entry in entries)
        sources = ", ".join(entry[0] for entry in entries)
        facts.append(f"{label} · {currency}: {total:,.2f} across {len(entries)} rows [{sources}]")
    largest_by_currency = {}
    for row in outflows:
        largest_by_currency.setdefault(row[4], row)
        if -row[3] > -largest_by_currency[row[4]][3]:
            largest_by_currency[row[4]] = row
    for currency, row in sorted(largest_by_currency.items()):
        ref, tx_date, desc, amount, _ = row
        facts.append(f"Largest outflow · {tx_date} · {desc[:160]} · {currency} {-amount:,.2f} [{ref}]")
    facts.append(f"Parsed {len(rows)} transactions; {len(outflows)} verified outflows.")
    return rows, {f"E{i}": fact for i, fact in enumerate(facts, 1)}


def evidence(documents):
    """Return parsed rows together with the keyed facts derived from those rows."""
    rows = transactions_from_documents(documents)
    return rows, overview(rows)[1]


def explicit_exclusions(question):
    """Preserve plainly stated category and merchant exclusions from the question."""
    found = {"exclude_categories": [], "exclude_merchants": []}
    pattern = re.compile(
        r"\b(?:excluding|exclude|without|but not|do not include|don't include|dont include|not including)\s+"
        r"([^,.;?]+)",
        re.I,
    )
    for match in pattern.finditer(question):
        value = match.group(1).strip().rstrip("!").strip()
        value = re.sub(r"\s+(?:only|please)$", "", value, flags=re.I).strip()
        if not value:
            continue
        category = CATEGORY_ALIASES.get(value.casefold())
        if category:
            found["exclude_categories"].append(category)
        else:
            found["exclude_merchants"].append(value[:80])
    return found


def current_category(question):
    """Find a supported category alias while ignoring text that is explicitly excluded."""
    normalized = re.sub(
        r"\b(?:excluding|exclude|without|but not|do not include|don't include|dont include|not including)\s+"
        r"[^,.;?]+",
        "",
        question.casefold(),
    )
    candidates = sorted(CATEGORY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, category in candidates:
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
            return category
    return None


def current_merchant(question, rows):
    """Use a merchant phrase only when it matches a description found in the data."""
    match = re.search(
        r"\b(?:at|to|on|for)\s+(.+?)(?=\b(?:excluding|exclude|without|but not|do not include|"
        r"don't include|dont include|not including|in|during|between|since)\b|[?.!,;:]|$)",
        question,
        re.I,
    )
    if not match:
        return None
    merchant = match.group(1).strip().strip("\"' ")
    if not merchant or CATEGORY_ALIASES.get(merchant.casefold()) or merchant.casefold() in {
        "food", "spending", "transactions", "expenses", "outflows",
    }:
        return None
    if any(merchant.casefold() in row[2].casefold() for row in rows):
        return merchant[:80]
    return None


def apply_question_constraints(query, question, rows):
    """Reapply clear user filters and turn ambiguous food/date scopes into clarifications."""
    category = current_category(question)
    if category:
        query["category"] = category
    merchant = current_merchant(question, rows)
    if merchant:
        query["merchant"] = merchant
    exclusions = explicit_exclusions(question)
    if any(exclusions.values()):
        query.update(exclusions)
    metric = infer_metric(question)
    if metric:
        query["metric"] = metric
    has_specific_food_scope = bool(category and category in {"Groceries", "Dining and takeaway"})
    asks_ambiguous_food = bool(re.search(r"\bfood\b", question, re.I)) and not has_specific_food_scope
    if asks_ambiguous_food:
        query.update(
            status="clarify",
            metric=None,
            message="“Food” could mean groceries or dining and takeaway. Please specify which category or categories to include.",
        )
    elif query.get("status") == "clarify" and metric and (category or merchant):
        query.update(status="ok", message=None)
    # A month without a year is safe only when all parsed statement dates share one year.
    if query.get("status") == "ok" and not re.search(r"\b(?:19|20)\d{2}\b", question):
        months = {
            MONTHS[match.casefold()]
            for match in re.findall(
                r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
                r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
                question,
                re.I,
            )
        }
        if len(months) == 1:
            years = {
                parsed.year
                for row in rows
                if (parsed := transaction_date(row[1])) is not None
            }
            if len(years) != 1:
                query.update(
                    status="clarify",
                    message="Please specify the year for that month; the statement dates do not identify one unambiguously.",
                )
            else:
                month = months.pop()
                year = years.pop()
                query["date_from"] = Date(year, month, 1)
                query["date_to"] = Date(year, month, monthrange(year, month)[1])
    return query


def infer_metric(question):
    """Recognize only the small set of metrics supported by the local query engine."""
    if re.search(r"\b(most frequent|most often|paid most often|highest number of times)\b", question, re.I):
        return "most_frequent"
    if re.search(r"\b(how many|number of|count of|count transactions)\b", question, re.I):
        return "count"
    if re.search(r"\b(largest|biggest|highest|most expensive)\b", question, re.I):
        return "largest"
    if re.search(r"\b(how much|total|sum|spent|spend|went to|paid)\b", question, re.I):
        return "sum"
    return None


def local_query(question, rows):
    """Build a conservative offline query; leave uncertain requests as clarification."""
    query = {
        "status": "clarify",
        "metric": infer_metric(question),
        "category": current_category(question),
        "merchant": current_merchant(question, rows),
        "date_from": None,
        "date_to": None,
        "currency": None,
        "exclude_categories": [],
        "exclude_merchants": [],
        "message": "Please specify a supported metric and category.",
    }
    query = apply_question_constraints(query, question, rows)
    if query["status"] != "ok":
        query["message"] = (
            "Groq is unavailable, and I couldn't safely resolve the query locally. "
            "Please specify a supported metric and a clear category or merchant."
        )
    return query


def execute_query(rows, query):
    """Apply validated filters and calculate source-cited results without model arithmetic."""
    if query.get("status") != "ok":
        return {
            "status": query.get("status", "unsupported"),
            "message": query.get("message") or "I could not safely interpret that question. Please clarify the metric or filters.",
            "facts": [],
            "tips": [],
        }
    metric = query["metric"]
    category = query.get("category")
    merchant = query.get("merchant")
    start = query.get("date_from")
    end = query.get("date_to")
    if start and end and start > end:
        return {"status": "clarify", "message": "The requested date range is reversed. Please clarify it.", "facts": [], "tips": []}
    # Refuse date filtering if any spending date cannot be normalized reliably.
    if start or end:
        if any(transaction_date(row[1]) is None for row in rows if is_spending_outflow(row)):
            return {
                "status": "clarify",
                "message": "Some transaction dates could not be normalized, so I cannot apply that date filter reliably.",
                "facts": [],
                "tips": [],
            }
    exclude_categories = set(query.get("exclude_categories", []))
    exclude_merchants = [value.casefold() for value in query.get("exclude_merchants", [])]
    matches = []
    for row in rows:
        _, raw_date, description, _, currency = row
        if not is_spending_outflow(row):
            continue
        tx_category = categorize(description)
        if category and tx_category != category:
            continue
        if merchant and merchant.casefold() not in description.casefold():
            continue
        if tx_category in exclude_categories:
            continue
        if any(exclusion in description.casefold() for exclusion in exclude_merchants):
            continue
        if query.get("currency") and currency != query["currency"]:
            continue
        tx_date = transaction_date(raw_date)
        if start and (tx_date is None or tx_date < start):
            continue
        if end and (tx_date is None or tx_date > end):
            continue
        matches.append(row)
    if not matches:
        return {
            "status": "unsupported",
            "message": "No matching transactions provide verifiable evidence. This is not a zero total.",
            "facts": [],
            "tips": [],
        }

    # Group before aggregating so values in different currencies are never combined.
    grouped = defaultdict(list)
    for row in matches:
        grouped[row[4]].append(row)
    facts = []
    if metric == "sum":
        for currency, entries in sorted(grouped.items()):
            total = sum((-row[3] for row in entries), Decimal("0"))
            facts.append(f"Verified outflows · {currency} {total:,.2f} · {len(entries)} rows [{', '.join(row[0] for row in entries)}]")
    elif metric == "count":
        for currency, entries in sorted(grouped.items()):
            facts.append(f"Verified outflow count · {currency} · {len(entries)} rows [{', '.join(row[0] for row in entries)}]")
    elif metric == "largest":
        for currency, entries in sorted(grouped.items()):
            row = max(entries, key=lambda item: -item[3])
            facts.append(f"Largest verified outflow · {row[1]} · {row[2]} · {currency} {-row[3]:,.2f} [{row[0]}]")
    elif metric == "most_frequent":
        for currency, entries in sorted(grouped.items()):
            counts = Counter(re.sub(r"\s+", " ", row[2]).strip().casefold() for row in entries)
            most_common = counts.most_common()
            top = most_common[0][1]
            for description_key, count in most_common:
                if count != top:
                    break
                matching_rows = [row for row in entries
                                 if re.sub(r"\s+", " ", row[2]).strip().casefold() == description_key]
                description = matching_rows[0][2]
                facts.append(f"Most frequent exact description · {description} · {currency} · {count} rows "
                             f"[{', '.join(row[0] for row in matching_rows)}]")
    else:
        return {"status": "unsupported", "message": "That metric is not supported.", "facts": [], "tips": []}
    message = "Results are grouped by currency. Merchant frequency uses exact scrubbed descriptions; similar merchant name variants may appear separately."
    return {"status": "ok", "message": message, "facts": facts, "tips": []}
