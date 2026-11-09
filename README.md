# Statement Scrubber

Statement Scrubber is a local web app for reviewing bank statements. It cleans supported files, saves the cleaned text, calculates spending totals, and lets you ask questions about transactions.

Python calculates all financial totals. The Groq service is optional and helps interpret questions or phrase answers using facts calculated by the app.

## Setup

You need Python 3.11 or newer and Docker Compose. The database can also be an existing PostgreSQL server. A Groq API key is only needed for AI-assisted features.

Create a `.env` file in the project folder with your database URL. Add a Groq key if you want to use those features:

```text
DATABASE_URL=postgresql://scrubber:admin@localhost:5432/scrubber
GROQ_API_KEY=your_groq_api_key
```

Keep `.env` private and never commit it. The database values must match the settings in `compose.yaml`. The example password is for local development only.

### Windows PowerShell

```powershell
docker compose up -d db
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

### macOS or Linux

```bash
docker compose up -d db
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

If you already have a PostgreSQL volume with a different password, update the local database role to match `DATABASE_URL`. This changes the password without deleting data:

```powershell
docker compose exec -u postgres db psql -d scrubber -c "ALTER ROLE scrubber WITH PASSWORD 'admin';"
```

The app creates its tables from `schema.sql` when it starts. Open <http://127.0.0.1:8000> to use it.

## Usage

1. Upload PDF, DOCX, CSV, or TXT statements. You can upload up to 20 files, with a combined upload size under 24 MB.
2. Review and download the cleaned text before generating an analysis.
3. Generate an analysis, then ask follow-up questions about supported transactions.

You can also clean a folder from the command line:

```powershell
python scrub.py C:\path\to\statements -o combined-scrubbed.txt
```

The folder tool processes files in sorted order, including files in subfolders. Run the tests with:

```powershell
python -m unittest discover -s tests -v
```

## How it works

The browser sends files to a FastAPI app. Python extracts their text and applies pattern-based redaction. The app saves the cleaned text in PostgreSQL under a batch ID, then shows a preview so you can check it.

The Python ledger parses supported transactions and calculates exact totals with `Decimal`, keeping currencies separate. For analysis and chat, the app may send computed facts, your question, and limited context such as available currencies or date ranges to Groq. It does not send the original uploaded files or full statement text to Groq, and it does not ask the model to calculate totals. Model responses are checked against locally calculated results; local answers remain available if Groq cannot be used.

PostgreSQL stores each upload batch, its cleaned documents, and the latest analysis. Psycopg connects the Python app to the database using direct, parameterized SQL.

## Approach and limitations

Statement files are processed locally, but uploads may be temporarily spooled to disk while the server handles them. The app stores cleaned text, not the original file contents. Redaction uses patterns and cannot guarantee that all personal information is removed, so review the preview before relying on it.

The parser supports common text-based PDF, DOCX, CSV, and TXT statements, but unfamiliar layouts may not parse correctly. Image-only PDFs need OCR, which is not included. Transaction categories are based on simple patterns, and unsupported rows may be missed. Check important totals against the original statement.

This project is intended for a single-user local demo, not public deployment or financial advice. The sample database credentials are not suitable for production. Running `docker compose down -v` deletes the local database volume and its data.

## GitHUB Link: https://github.com/wahab5763/Scrubber.git
