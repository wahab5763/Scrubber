# R&D and design notes

## What I wanted to build

I wanted to make a local web app that can read bank statements, hide common personal details, calculate spending, and answer questions about transactions. While planning it, I thought about how to handle different file formats, protect statement data, and make sure the totals are trustworthy.

## Reading and cleaning statements

I considered using an online document service because it might support more formats. That would mean sending original statements to another service, so I chose to read files locally with `pdfplumber`, `python-docx`, and Python's CSV tools. The downside is that scanned PDFs need OCR, which this version does not include.

Removing every number would also remove dates and amounts. A general-purpose name finder might miss account details or hide useful merchant names. I used patterns that look at the surrounding text instead, then tested them with made-up examples. This keeps more transaction information, but it cannot catch everything. Users should check the cleaned preview.

## Saving data and building the API

I chose PostgreSQL to save each upload, its documents, and its latest analysis. Each upload gets a UUID, and documents keep their order in that upload. The database links related records so they can be cleaned up together.

I used Psycopg and direct SQL because the database is small and the app only needs a few queries. SQLAlchemy could be useful if the app grows and needs more models or complex queries.

The app cleans all files before saving them in one database transaction. If saving fails, the database should not be left with only part of an upload. Separate API routes handle the preview, storage details, analysis, and chat. If this became a production app, I would add database migrations and rules for how long statement data is kept.

## Calculations and AI

I did not want an AI model to calculate spending totals because its arithmetic can be hard to verify. Python parses supported transactions and uses `Decimal` for accurate amounts. It keeps different currencies separate and records where each transaction came from.

The app uses Groq's `openai/gpt-oss-20b` model to help understand questions and select or explain existing facts. It does not calculate the totals. The app asks the model for structured JSON, checks the response, and verifies its references against facts calculated locally. If the model is unavailable or gives an invalid response, the app can still show local results or ask for clarification.

For finding transactions, I chose regular PostgreSQL queries instead of adding a vector database and embeddings. If the app later needs broader, meaning-based document search, that could be reconsidered.

## Testing and limits

I added tests with made-up data for redaction, transaction parsing, exact totals, and invalid AI answers. Run them with `python -m unittest discover -s tests -v`. These tests cover examples, not every bank or every kind of personal information.

Before using this beyond a local demo, I would add more test statements, compare results with manually checked totals, support scanned PDFs with OCR and a review step, and add data-retention controls. The app cannot guarantee that all personal details are removed, and it is not a financial-advice tool.

## Note: After writing above data by myself, I just rephrases with AI to correct grammar and structure.
