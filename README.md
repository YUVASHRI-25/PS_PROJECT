# Student Skills WhatsApp Query Bot (PostgreSQL + pgvector + Groq)

This starter imports the supplied MySQL-style `ps_cleared_course_view` dump into PostgreSQL, answers natural-language questions with Groq-generated **read-only SQL**, and sends replies through the official WhatsApp Cloud API.

## Important data limitation
The source has these fields only: `roll_number`, `name`, `vertical_name`, `course_name`, `date`, `start_time`, `end_time`, `venue_name`, `status`. It has no explicit `skill`, `department`, GPA, or placement outcome columns. Questions such as "full stack" and "backend with Java" can only be approximated by matching course/vertical names unless you add a verified skill taxonomy. Also, each row is an assessment attempt; repeated rows are not necessarily separate students or unique skills.

## 1. Install PostgreSQL and pgAdmin
Install PostgreSQL for Windows (include Command Line Tools) and pgAdmin 4. Remember: pgAdmin's **master password** unlocks saved server passwords; it is not the PostgreSQL database user's password.

Open pgAdmin and connect to your local PostgreSQL server. Create database `skillsdb`.

## 2. Enable pgvector
Install pgvector for your exact PostgreSQL version/Windows build. If the installer/package you use does not include it, follow the official pgvector installation instructions for your platform. Then in pgAdmin Query Tool, connected to `skillsdb`, run:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

If PostgreSQL says it cannot open `vector.control`, pgvector is not installed into that PostgreSQL server installation yet. Do not continue to embeddings until fixed.

## 3. Create table and import the dump
Open `schema.sql` in pgAdmin Query Tool and execute it. Then create a dedicated read-only login for the bot (change the password):

```sql
CREATE ROLE skillbot_readonly LOGIN PASSWORD 'REPLACE_WITH_A_LONG_RANDOM_PASSWORD';
GRANT CONNECT ON DATABASE skillsdb TO skillbot_readonly;
GRANT USAGE ON SCHEMA public TO skillbot_readonly;
GRANT SELECT ON ps_cleared_course_view, course_embeddings TO skillbot_readonly;
```

Use a separate admin connection for importing. In PowerShell, from this project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your PostgreSQL connection string and Groq/WhatsApp credentials. For importing, temporarily set `DATABASE_URL` to your **admin** PostgreSQL connection in `.env` (or use a separate admin env file). Run:

```powershell
python import_mysql_dump.py "C:\path\to\ps_cleared_data.sql"
```

The script truncates the target table before importing, so use it only on a new/dedicated target table. The dump is around 46 MB and contains multiple INSERT blocks; allow it a few minutes. Verify in pgAdmin:

```sql
SELECT COUNT(*) FROM ps_cleared_course_view;
SELECT status, COUNT(*) FROM ps_cleared_course_view GROUP BY status;
SELECT * FROM ps_cleared_course_view LIMIT 5;
```

After importing, change `.env` back to the `skillbot_readonly` connection string.

## 4. Optional semantic index with pgvector
The bot uses SQL for exact counts/rankings; embeddings are helpful for finding related course names, not for counting students. Run:

```powershell
python embed_index.py
```

The first run downloads `all-MiniLM-L6-v2` (internet required). This creates embeddings for distinct vertical/course names. To query nearest matches in pgAdmin:

```sql
SELECT vertical_name, course_name, 1 - (embedding <=> (
  SELECT embedding FROM course_embeddings LIMIT 1
)) AS similarity
FROM course_embeddings
ORDER BY embedding <=> (
  SELECT embedding FROM course_embeddings LIMIT 1
)
LIMIT 10;
```

That is only a wiring test; replace the subquery with a parameterized embedding generated from the user's query for actual semantic retrieval. The initial bot uses Groq + guarded SQL and does not yet use vector similarity in its answer path.

## 5. Get Groq API key
Create an API key in the Groq console and put it in `.env` as `GROQ_API_KEY`. Do not commit `.env` or expose the key in browser/client code. The default `GROQ_MODEL` is `openai/gpt-oss-120b`, Groq's replacement for the retired `llama-3.3-70b-versatile` model. You can change `GROQ_MODEL` to any model currently available to your Groq account.

## 6. Configure WhatsApp Cloud API
In Meta for Developers:
1. Create an app and add the WhatsApp product.
2. Use the test sender/recipient setup first; add and verify your own recipient number as directed in the dashboard.
3. Copy the temporary/permanent access token and Phone Number ID into `.env`.
4. Deploy this FastAPI service on a public HTTPS host (or use a temporary HTTPS tunnel for local testing).
5. In WhatsApp Configuration, set the webhook callback URL to `https://YOUR_PUBLIC_HOST/webhook`, and the verify token to exactly `WHATSAPP_VERIFY_TOKEN` from `.env`.
6. Subscribe the app to the `messages` webhook field.
7. Send a message to the test WhatsApp number and check the app logs.

WhatsApp Cloud API is not the same as automating WhatsApp Web. Meta may require business/app setup and message-template rules for business-initiated conversations. For replies inside the customer service window, free-form replies are generally allowed subject to current Meta policy.

## 7. Run locally
```powershell
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
Open `http://127.0.0.1:8000/` to use the web dashboard. It sends questions to
the same read-only query pipeline as WhatsApp. The health endpoint is
`http://127.0.0.1:8000/health`. For webhook testing, expose port 8000 through
an HTTPS tunnel and configure that URL in Meta.

## 8. Example questions
- `How many distinct students have cleared Programming Java?`
- `Top 10 students with the most cleared Java-related courses`
- `Count students with cleared courses whose names mention Python`
- `How many students have cleared Database Programming?`
- `Show cleared vs not-cleared counts for Programming Java`

Be careful with "full stack" and "backend": these terms do not appear as structured fields in the provided schema. For reliable rankings, maintain an approved mapping table from courses to skill categories (e.g., backend, frontend, full stack, Java) rather than trusting fuzzy model interpretation.

## 9. pgAdmin password forgotten?
There are two different passwords:
- **pgAdmin master password**: unlocks pgAdmin's locally saved credentials. It does not reset the PostgreSQL login. Depending on your pgAdmin version, use its password-reset/credential-reset option or reset the local pgAdmin configuration; this can remove saved server connections/passwords.
- **PostgreSQL server password** (often the `postgres` role): if another database superuser can log in, run `ALTER ROLE postgres WITH PASSWORD 'new-long-password';`. If no superuser can log in, use the official PostgreSQL Windows password-reset procedure for your installed version. It usually involves a temporary local authentication change in `pg_hba.conf`; back up the file, restrict any temporary trust rule to localhost, restart the correct PostgreSQL service, set the new password, restore the original authentication rule immediately, and restart again. Never leave `trust` enabled. Do not uninstall PostgreSQL or delete its data directory to reset a password.

## Security notes
- Keep `.env` private; add it to `.gitignore`.
- Use the read-only DB role in the running bot.
- Review generated SQL before production; a read-only DB role is the strongest safeguard.
- Add webhook signature verification, per-user access controls, rate limiting, audit logging, and privacy approval before using real student records.
