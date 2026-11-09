import unittest

from ledger import evidence, execute_query, transactions_from_documents
from provider import validate
from scrub import extract, redact


class Scrubbing(unittest.TestCase):
    """Regression tests for redaction, parsing, arithmetic, and provider validation."""

    # Redaction should remove identifiers without damaging useful transaction context.
    def test_preserves_analysis_context(self):
        clean = redact("2026-04-05 11:32:00 Eva paid Netflix.com USD 12.00 Card xxxx1234")
        self.assertEqual(clean, "2026-04-05 11:32:00 Eva paid Netflix.com USD 12.00 Card xxxx1234")

    def test_contextual_and_inline_identifiers(self):
        clean = redact("  Customer Address: 123 Example Road\n"
                       "IBAN: GB29 NWBK 6016 1331 9268 19\n"
                       "Eva eva@example.org pays eva@wallet, +44 7700 900123\n"
                       "Card: 543212******1234; SSN: 987-65-4320\n"
                       "Sort code: 11-22-33 (112233) Account Number: 12345678")
        for sensitive in ("Example Road", "6016", "eva@", "7700", "543212", "987-65", "112233", "12345678"):
            self.assertNotIn(sensitive, clean)
        self.assertIn("Eva", clean)

    def test_csv_metadata_and_amount(self):
        clean = extract("input.csv", b"Wallet ID,secretWallet\nDate,Type,Description,Amount,Balance\n"
                        b"2026-04-01,Payment,Netflix.com,-12345678.01,12345678.02\n")
        self.assertNotIn("secretWallet", clean)
        self.assertIn("Netflix.com,-12345678.01,12345678.02", clean)

    def test_ledger_uses_decimal_and_no_cross_currency_sum(self):
        body = "2026-04-01 | Coffee | -£0.10 | £18.00\n2026-04-02 | Coffee | -£0.20 | £17.80"
        rows, facts = evidence([(1, "statement.docx", body)])
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("GBP: 0.30" in fact for fact in facts.values()))

    def test_chat_largest_outflow_excludes_larger_income(self):
        body = ("2026-04-01 | UBER TRIP | -£18.00 | £100.00\n"
                "2026-04-02 | TESCO GROCERIES | -£45.00 | £55.00\n"
                "2026-04-03 | SALARY ACME LTD | +£1,000.00 | £1,055.00")
        rows = transactions_from_documents([(1, "sample.docx", body)])
        result = execute_query(rows, {
            "status": "ok", "metric": "largest", "category": None, "merchant": None,
            "date_from": None, "date_to": None, "currency": None,
            "exclude_categories": [], "exclude_merchants": [],
        })
        self.assertIn("Largest verified outflow · 2026-04-02 · TESCO GROCERIES", result["facts"][0])
        self.assertFalse(any("SALARY ACME LTD" in fact for fact in result["facts"]))

    def test_overview_excludes_income_from_outflow_totals(self):
        body = ("2026-04-01 | ACME TRANSFER | -£18.00 | £100.00\n"
                "2026-04-02 | ACME TRANSFER | +£500.00 | £600.00")
        _, facts = evidence([(1, "sample.docx", body)])
        self.assertIn("All outflows · GBP: 18.00 across 1 rows [F1:1]", facts.values())
        self.assertFalse(any("GBP 500.00" in fact for fact in facts.values()))

    def test_rejects_invented_evidence_or_extra_amount(self):
        for answer in ('{"status":"ok","evidence":["E9"],"tips":[]}',
                       '{"status":"ok","evidence":["E1"],"tips":[],"amount":999}',
                       '{"status":"ok","evidence":["E1"],"tips":["made-up"]}'):
            with self.assertRaises(ValueError):
                validate(answer, {"E1": "GBP 2.00"})

    def test_renders_only_local_facts_and_handles_refusal(self):
        self.assertEqual(validate('{"status":"ok","evidence":["E1"],"tips":[]}',
                                  {"E1": "GBP 2.00"})["facts"], ["GBP 2.00"])
        self.assertFalse(validate('{"status":"unsupported","evidence":[],"tips":[]}', {})["facts"])


if __name__ == "__main__":
    unittest.main()
