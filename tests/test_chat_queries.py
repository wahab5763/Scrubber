import json
import unittest
from datetime import date
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import app
from ledger import execute_query, overview, transactions_from_documents
import provider
from provider import translate_question


# Synthetic statement rows keep chat tests deterministic and free of real financial data.
STATEMENT = """2026-03-01 | UBER TRIP | -£8.00 | £200.00
2026-03-02 | UBER TRIP | -£9.00 | £191.00
2026-04-01 | TESCO STORES GROCERIES | -£45.00 | £146.00
2026-04-02 | WHOLE FOODS MARKET | -£30.00 | £116.00
2026-04-03 | CAFE CENTRAL | -£12.00 | £104.00
2026-04-04 | FOODPANDA TAKEAWAY | -£20.00 | £84.00
2026-04-05 | NETFLIX.COM | -£15.00 | £69.00
2026-04-06 | SPOTIFY UK | -£10.00 | £59.00
2026-04-07 | SPOTIFY UK | -£10.00 | £49.00"""
DOCUMENTS = [(1, "synthetic.docx", STATEMENT)]
ROWS = transactions_from_documents(DOCUMENTS)


def model_query(**overrides):
    """Build a valid default model response, with selected fields overridden per test."""
    query = {
        "status": "ok",
        "metric": "sum",
        "category": None,
        "merchant": None,
        "date_from": None,
        "date_to": None,
        "currency": None,
        "exclude_categories": [],
        "exclude_merchants": [],
        "message": None,
    }
    query.update(overrides)
    return json.dumps(query)


class FakeDatabase:
    """Provide only the context-manager and query methods used by the API tests."""

    def __init__(self, documents):
        self.documents = documents
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, _params=None):
        self.statements.append(query)
        self.last_params = _params
        return self

    def fetchall(self):
        return self.documents


class ChatQueries(unittest.TestCase):
    """Test filter interpretation, local calculations, fallbacks, and evidence checks."""

    def chat(self, question, response, history=None):
        # Simulate both model stages while routing database reads to the in-memory fixture.
        db = FakeDatabase(DOCUMENTS)
        def fake_request(messages, max_tokens=800, response_schema=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("task") == "chat_answer":
                facts = payload["evidence"]
                return json.dumps({
                    "answer": "Verified statement results: " + "; ".join(facts.values()),
                    "fact_ids": list(facts),
                })
            return response

        with patch("app.connection", return_value=db), patch("provider._request", side_effect=fake_request):
            result = app.chat(uuid4(), app.Question(
                question=question,
                previous_questions=history or [],
            ))
        return result

    def test_groceries_only_excludes_dining(self):
        # Explicit words in the current question must win over conflicting model filters.
        result = self.chat(
            "How much for groceries only? Don't include dining.",
            model_query(category="Dining and takeaway", exclude_merchants=["TESCO"]),
            history=["How much did I spend at Tesco?"],
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("GBP 75.00", result["facts"][0])
        self.assertNotIn("CAFE CENTRAL", " ".join(result["facts"]))
        self.assertNotIn("FOODPANDA", " ".join(result["facts"]))

    def test_followup_with_explicit_dining_scope_recovers_invalid_model_query(self):
        response = json.dumps({
            "status": "invalid",
            "metric": "not-a-supported-metric",
            "unexpected": True,
        })
        result = self.chat(
            "How much went to food? only Dining and takeaway",
            response,
            history=["How much went to food?"],
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["facts"], ["Verified outflows · GBP 32.00 · 2 rows [F1:5, F1:6]"])

    def test_unqualified_food_remains_ambiguous(self):
        result = self.chat(
            "How much went to food?",
            model_query(),
        )
        self.assertEqual(result["status"], "clarify")
        self.assertIn("could mean groceries or dining", result["message"])

    def test_subscription_category_and_merchant_exclusion(self):
        result = self.chat(
            "How much did I spend on subscriptions excluding Netflix?",
            model_query(category="Subscriptions", exclude_merchants=["Netflix"]),
        )
        self.assertEqual(result["facts"], ["Verified outflows · GBP 20.00 · 2 rows [F1:8, F1:9]"])
        self.assertNotIn("NETFLIX", " ".join(result["facts"]))

    def test_april_frequent_description_uses_dates_and_discloses_grouping_limit(self):
        result = self.chat(
            "Which merchant did I pay most often in April?",
            model_query(metric="most_frequent"),
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("Merchant frequency uses exact scrubbed descriptions", result["message"])
        self.assertNotIn("UBER TRIP", " ".join(result["facts"]))
        self.assertTrue(any("SPOTIFY UK" in fact for fact in result["facts"]))

    def test_month_without_unique_year_requests_clarification(self):
        db = FakeDatabase([(1, "two-years.docx", "2025-04-01 | MARKET | -£10.00 | £90.00\n"
                           "2026-04-01 | MARKET | -£12.00 | £78.00")])
        response = model_query(metric="sum")
        with patch("app.connection", return_value=db), patch("provider._request", return_value=response):
            result = app.chat(uuid4(), app.Question(question="How much did I spend in April?"))
        self.assertEqual(result["status"], "clarify")
        self.assertIn("specify the year", result["message"])

    def test_category_only_query_discards_model_inferred_statement_date_bounds(self):
        response = model_query(
            category="Subscriptions",
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
        with patch("provider._request", return_value=response):
            query = translate_question("how much went to Subscriptions ?", [], ROWS)
        self.assertIsNone(query["date_from"])
        self.assertIsNone(query["date_to"])

    def test_explicit_month_query_keeps_model_date_scope(self):
        response = model_query(
            category="Subscriptions",
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
        with patch("provider._request", return_value=response):
            query = translate_question("How much went to subscriptions in April 2026?", [], ROWS)
        self.assertEqual(query["date_from"], date(2026, 4, 1))
        self.assertEqual(query["date_to"], date(2026, 4, 30))

    def test_missing_tax_is_not_reported_as_zero(self):
        result = self.chat("What was my total tax?", model_query(category="Tax"))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("not a zero total", result["message"])
        self.assertEqual(result["facts"], [])

    def test_chat_falls_back_to_local_merchant_query_on_groq_rate_limit(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        db = FakeDatabase(DOCUMENTS)
        with patch("app.connection", return_value=db), \
                patch("provider.translate_question", side_effect=error), \
                patch("provider.compose_chat_answer", side_effect=error):
            result = app.chat(uuid4(), app.Question(question="How much went to Netflix?"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer_source"], "local")
        self.assertIn("rate limit reached", result["provider_status"])
        self.assertEqual(result["facts"], ["Verified outflows · GBP 15.00 · 1 rows [F1:7]"])

    def test_unclear_query_returns_clarification_not_service_unavailable(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        db = FakeDatabase(DOCUMENTS)
        with patch("app.connection", return_value=db), patch("provider.translate_question", side_effect=error):
            result = app.chat(uuid4(), app.Question(question="Tell me something about spending"))
        self.assertEqual(result["status"], "clarify")
        self.assertEqual(result["mode"], "clarification")
        self.assertIn("rate limit reached", result["provider_status"])

    def test_provider_error_includes_safe_groq_http_detail(self):
        response = httpx.Response(
            400,
            json={"error": {"message": "response format is not supported"}},
            request=httpx.Request("POST", "https://api.groq.com"),
        )
        error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
        self.assertEqual(
            app.provider_error(error),
            "Groq rejected the request (HTTP 400): response format is not supported",
        )

    def test_ambiguous_numeric_date_filter_requests_clarification(self):
        rows = transactions_from_documents([
            (1, "unknown-country.docx", "04/01/2026 | MARKET | -$10.00 | $90.00")
        ])
        result = execute_query(rows, {
            "status": "ok", "metric": "sum", "category": None, "merchant": None,
            "date_from": date(2026, 4, 1),
            "date_to": date(2026, 4, 30), "currency": None,
            "exclude_categories": [], "exclude_merchants": [],
        })
        self.assertEqual(result["status"], "clarify")
        self.assertIn("could not be normalized", result["message"])

    def test_numeric_date_convention_is_scoped_to_statement_section(self):
        body = (
            "Harborstone National Bank (USA)\n"
            "04/01/2026 | MARKET USA | -$10.00 | $90.00\n"
            "Rheinbank AG (Germany)\n"
            "01.04.2026 | MARKT BERLIN | -€8.00 | €92.00"
        )
        rows = transactions_from_documents([(1, "combined.docx", body)])
        self.assertEqual(rows[0][1], "2026-04-01")
        self.assertEqual(rows[1][1], "01.04.2026")

    def test_currency_context_and_payment_reversal_are_handled(self):
        rows = transactions_from_documents([(
            1,
            "us_statement.docx",
            "Harborstone National Bank (USA)\n"
            "04/01/2026 | MARKET | -$10.00 | $90.00\n"
            "04/02/2026 | Fiverr International Ltd Payment Reversal | -$85.00 | $5.00",
        )])
        self.assertEqual([row[4] for row in rows], ["USD", "USD"])
        self.assertEqual([row[0] for row in rows if row[3] < 0 and "reversal" not in row[2].casefold()], ["F1:2"])
        _, facts = overview(rows)
        self.assertIn("All outflows · USD: 10.00 across 1 rows [F1:2]", facts.values())

    def test_explicit_tax_payment_can_be_summed(self):
        tax_rows = transactions_from_documents([
            (1, "tax.docx", "2026-04-01 | WITHHOLDING TAX PAYMENT | -£12.00 | £88.00")
        ])
        result = execute_query(tax_rows, {
            "status": "ok", "metric": "sum", "category": "Tax",
            "merchant": None, "date_from": None, "date_to": None, "currency": None,
            "exclude_categories": [], "exclude_merchants": [],
        })
        self.assertEqual(result["facts"], ["Verified outflows · GBP 12.00 · 1 rows [F1:1]"])

    def test_query_schema_rejects_unapproved_metric_and_filter(self):
        for response in (
            model_query(metric="average"),
            model_query(exclude_merchants=["Netflix"], unexpected="value"),
        ):
            with self.subTest(response=response), patch("provider._request", return_value=response):
                self.assertEqual(translate_question("How much?", [], ROWS)["status"], "clarify")

    def test_request_uses_supported_chat_completion_parameters(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        from provider import CHAT_ANSWER_SCHEMA
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
                patch("provider.httpx.post", return_value=response) as post:
            from provider import _request
            self.assertEqual(
                _request([{"role": "user", "content": "test"}], response_schema=CHAT_ANSWER_SCHEMA),
                '{"ok": true}',
            )
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {
            "type": "json_schema",
            "json_schema": {
                "name": "provider_response",
                "strict": True,
                "schema": CHAT_ANSWER_SCHEMA,
            },
        })
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("include_reasoning", payload)

    def test_chat_answer_uses_conversational_groq_text_with_citations(self):
        response = json.dumps({
            "answer": "You spent GBP 15.00 on Netflix.",
            "fact_ids": ["E1"],
        })
        facts = {"E1": "Verified outflows · GBP 15.00 · 1 row [F1:7]"}
        with patch("provider._request", return_value=response) as request:
            answer = provider.compose_chat_answer("How much went to Netflix?", [], facts)
        self.assertEqual(answer, {
            "message": "You spent GBP 15.00 on Netflix.",
            "fact_ids": ["E1"],
        })
        self.assertEqual(request.call_args.kwargs["max_tokens"], 1200)

    def test_chat_answer_uses_pre_filtered_grocery_facts(self):
        response = json.dumps({
            "answer": "Groceries, excluding dining: CAD 118.92, GBP 54.20, PKR 7,340.50, USD 132.60, and Unspecified 6,320.40.",
            "fact_ids": ["E1", "E2", "E3", "E4", "E5"],
        })
        facts = {
            "E1": "Verified outflows · CAD 118.92 · 1 rows [F2:51]",
            "E2": "Verified outflows · GBP 54.20 · 1 rows [F2:12]",
            "E3": "Verified outflows · PKR 7,340.50 · 1 rows [F1:24]",
            "E4": "Verified outflows · USD 132.60 · 1 rows [F2:32]",
            "E5": "Verified outflows · Unspecified (file 4) 6,320.40 · 1 rows [F4:21]",
        }
        with patch("provider._request", return_value=response) as request:
            answer = provider.compose_chat_answer(
                "How much for groceries only? Don't include dining",
                [],
                facts,
                {"metric": "sum", "category": "Groceries", "exclude_categories": ["Dining and takeaway"]},
            )
        self.assertIn("CAD 118.92", answer["message"])
        system_prompt = request.call_args.args[0][0]["content"]
        self.assertIn("locally validated filters", system_prompt)
        payload = json.loads(request.call_args.args[0][1]["content"])
        self.assertEqual(payload["verified_filters"]["category"], "Groceries")

    def test_chat_calls_groq_for_query_and_natural_language_answer(self):
        requests = []

        def fake_request(messages, max_tokens=800, response_schema=None):
            payload = json.loads(messages[-1]["content"])
            requests.append(payload)
            if payload.get("task") == "chat_answer":
                return json.dumps({
                    "answer": "You spent GBP 15.00 on Netflix.",
                    "fact_ids": ["E1"],
                })
            return model_query(category="Subscriptions", merchant="Netflix")

        db = FakeDatabase(DOCUMENTS)
        with patch("app.connection", return_value=db), patch("provider._request", side_effect=fake_request):
            result = app.chat(uuid4(), app.Question(question="How much went to Netflix?"))
        self.assertEqual(result["message"], "You spent GBP 15.00 on Netflix.")
        self.assertEqual(result["facts"], ["Verified outflows · GBP 15.00 · 1 rows [F1:7]"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["current_question"], "How much went to Netflix?")
        self.assertEqual(requests[1]["task"], "chat_answer")
        self.assertEqual(requests[1]["verified_filters"]["category"], "Subscriptions")

    def test_chat_answer_rejects_invented_amount(self):
        response = json.dumps({
            "answer": "You spent GBP 99.00.",
            "fact_ids": ["E1"],
        })
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer(
                    "How much?", [], {"E1": "Verified outflows · GBP 15.00 · 1 row [F1:7]"}
                )

    def test_chat_answer_allows_a_date_repeated_from_the_question(self):
        response = json.dumps({
            "answer": "On 1 April 2026, verified outflows were Unspecified 1,740.50 across 2 rows.",
            "fact_ids": ["E1"],
        })
        facts = {"E1": "Verified outflows · Unspecified (file 4) 1,740.50 · 2 rows [F4:13, F4:14]"}
        with patch("provider._request", return_value=response):
            answer = provider.compose_chat_answer("How much went on 1 April 2026?", [], facts)
        self.assertIn("1 April 2026", answer["message"])

    def test_chat_answer_rejects_a_date_not_in_question_or_evidence(self):
        response = json.dumps({
            "answer": "On 2 April 2026, verified outflows were Unspecified 1,740.50 across 2 rows.",
            "fact_ids": ["E1"],
        })
        facts = {"E1": "Verified outflows · Unspecified (file 4) 1,740.50 · 2 rows [F4:13, F4:14]"}
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer("How much went on 1 April 2026?", [], facts)

    def test_chat_answer_rejects_invented_currency(self):
        response = json.dumps({
            "answer": "You spent USD 15.00.",
            "fact_ids": ["E1"],
        })
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer(
                    "How much?", [], {"E1": "Verified outflows · GBP 15.00 · 1 row [F1:7]"}
                )

    def test_chat_answer_must_cite_all_currency_results(self):
        response = json.dumps({
            "answer": "You spent GBP 15.00.",
            "fact_ids": ["E1"],
        })
        facts = {
            "E1": "Verified outflows · GBP 15.00 · 1 row [F1:7]",
            "E2": "Verified outflows · EUR 15.99 · 1 row [F2:9]",
        }
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer("How much?", [], facts)

    def test_chat_answer_rejects_refusal_when_matching_totals_exist(self):
        response = json.dumps({
            "answer": "I don't have evidence about fees.",
            "fact_ids": ["E1"],
        })
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer(
                    "How much went to fees?",
                    [],
                    {"E1": "Verified outflows · GBP 89.00 · 1 rows [F1:13]"},
                )

    def test_chat_answer_rejects_curly_apostrophe_refusal(self):
        response = json.dumps({
            "answer": "I don’t have enough information to determine the grocery total.",
            "fact_ids": ["E1"],
        })
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer(
                    "How much for groceries?",
                    [],
                    {"E1": "Verified outflows · GBP 54.20 · 1 rows [F1:12]"},
                )

    def test_chat_refusal_with_matching_rows_falls_back_to_local_summary(self):
        db = FakeDatabase(DOCUMENTS)
        query_response = model_query(category="Subscriptions", merchant="Netflix")
        refusal = json.dumps({
            "answer": "I don't have enough information about Netflix.",
            "fact_ids": ["E1"],
        })
        def fake_request(messages, max_tokens=800, response_schema=None):
            payload = json.loads(messages[-1]["content"])
            return refusal if payload.get("task") == "chat_answer" else query_response

        with patch("app.connection", return_value=db), patch("provider._request", side_effect=fake_request):
            result = app.chat(uuid4(), app.Question(question="How much went to Netflix?"))
        self.assertEqual(result["answer_source"], "local")
        self.assertIn("GBP 15.00", result["message"])
        self.assertIn("Groq returned an invalid response", result["provider_status"])

    def test_chat_answer_must_mention_unspecified_currency_and_every_sum(self):
        facts = {
            "E1": "Verified outflows · GBP 89.00 · 1 rows [F1:13]",
            "E2": "Verified outflows · Unspecified (file 4) 14,620.00 · 5 rows [F4:14]",
        }
        response = json.dumps({
            "answer": "Utilities were GBP 89.00 and 14,620.00.",
            "fact_ids": ["E1", "E2"],
        })
        with patch("provider._request", return_value=response):
            with self.assertRaises(ValueError):
                provider.compose_chat_answer("How much went to utilities?", [], facts)

    def test_local_totals_survive_simulated_groq_refusal(self):
        refusal = json.dumps({
            "status": "unsupported",
            "evidence": [],
            "tips": [],
        })
        with patch("provider._request", return_value=refusal):
            result = app.analysis_result(DOCUMENTS)
        self.assertEqual(result["status"], "local_only")
        self.assertGreater(result["parsed_rows"], 0)
        self.assertTrue(any("All outflows · GBP: 159.00" in fact for fact in result["facts"]))
        self.assertIn("retry Generate analysis", result["message"])

    def test_cached_refusal_does_not_hide_locally_computed_analysis(self):
        db = FakeDatabase(DOCUMENTS)
        refusal = json.dumps({"status": "unsupported", "evidence": [], "tips": []})
        with patch("app.connection", return_value=db), patch("provider._request", return_value=refusal):
            result = app.analyze(uuid4())
        self.assertEqual(result["status"], "local_only")
        self.assertTrue(any("All outflows · GBP: 159.00" in fact for fact in result["facts"]))
        self.assertTrue(any("ON CONFLICT (batch_id) DO UPDATE" in sql for sql in db.statements))
        self.assertFalse(any("SELECT result FROM analyses" in sql for sql in db.statements))


if __name__ == "__main__":
    unittest.main()
