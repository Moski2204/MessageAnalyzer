# Instagram Message Analyzer

A private, local Flask application for importing, searching, browsing, and
measuring one Instagram HTML conversation export. The app reads
`data/message_*.html`, builds a local SQLite/FTS5 database, and never uploads
message data.

This copy is configured to import only messages whose sender label is
`Mahrus` or `🐧`. Messages attributed to Meta AI or any other sender label are
excluded from the generated database, search, conversation browser, reports,
and analysis.

## Install on Windows

From this project folder in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks activation, you can run the virtual environment's Python
directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Start

```powershell
python app.py
```

Open <http://127.0.0.1:5000>. The server binds only to `127.0.0.1`, and debug
mode is off.

Hot reload is enabled by default. Changes to Python files, templates, CSS, or
local JavaScript restart Flask and automatically reload open app pages. To
disable it for a run:

```powershell
$env:MESSAGE_ANALYZER_HOT_RELOAD="0"
python app.py
```

On Home, select **Import Messages**. The import automatically discovers and
numerically sorts every `data/message_*.html` file. It may take several minutes
for a very large export. The original `data/` files are never changed.

Keep the private export in this local layout:

```text
data/
  message_1.html
  message_2.html
  ...
  photos/
    <Instagram photo files>
```

The entire `data/` directory is ignored by Git. Do not move the photos into
`static/`: the app serves verified local images through its restricted
`/photos/<filename>` route. During import, photo references are matched once
against `data/photos`, and SQLite stores only paths relative to that directory.
Missing or ambiguous matches are not guessed and appear as **Photo
unavailable**.

You can also import without opening the web page:

```powershell
python app.py --import-data
```

## Use

- **Search Conversation** supports contains-text, exact-phrase, all-word, and
  any-word modes, plus sender/date filters, chronological sorting, and result
  pagination.
- **Search Report** shows the match and its nearby messages. **View in Full
  Conversation** opens the page containing that database message and
  highlights it.
- **Full Conversation** pages through 50, 100, or 250 messages without loading
  the whole export into the browser. Photos are loaded only for messages on the
  current page and use browser lazy loading.
- **Analysis** accepts an optional date range and editable stop-word list. It
  shows response timing, frequent words, approximate local sentiment, and
  descriptive conversation patterns.

Response time is approximate. Consecutive messages from one sender form a run;
when the sender changes, time is measured from the last message of the prior
run to the first message in the new run.

Frequent-word analysis removes URLs, punctuation-only tokens, media
placeholders, and the editable stop words. It does not alter stored messages.

Sentiment uses the local `vaderSentiment` package. It is automated,
English-oriented, and can misunderstand context, sarcasm, jokes, slang,
emojis, Urdu, Arabic, Roman Urdu, and other non-English text. Its labels do not
prove hostility, harm, affection, sincerity, or intent.

Conversation-pattern measurements describe only message counts, timing,
lengths, questions, gaps, and approximate sentiment. They do not establish
feelings, motives, honesty, compatibility, attraction, manipulation, or who
cares more.

## Offline reports

Use **Download Report** on a Search Report to create a complete, self-contained
HTML search report. Use **Download Analysis Report** on Analysis for a summary
report. Reports are saved in `reports/` and downloaded by the browser. They
contain inline CSS, no external scripts, and no tracking.

## Database and rebuilding

The generated database is `instance/messages.db`. **Rebuild Database** deletes
only that generated database and recreates it from the original HTML files.
It never deletes or edits anything in `data/`.

To rebuild from PowerShell:

```powershell
python app.py --rebuild
```

The Instagram message timestamps in this export do not include timezone
information. The app preserves the original timestamp text and treats its
clock fields as a stable timezone-neutral ordering value.
