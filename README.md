# Instagram Message Analyzer

A private, local Flask viewer for an existing Instagram conversation database.
The app searches, browses, measures, and reports on the SQLite database at
`instance/messages.db`; it does not upload message data.

This copy is configured to show only messages whose sender label is `Mahrus` or
`🐧`. Messages attributed to Meta AI or any other sender label are excluded
from the database viewer, search, conversation browser, reports, and analysis.

## Install on Windows

From this project folder in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks activation, you can install dependencies with the virtual
environment's Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Start

For normal use, run:

```powershell
.\.venv\Scripts\python.exe app.py
```

Open <http://127.0.0.1:5000>. The server binds only to `127.0.0.1`; debug mode
and hot reload are off. Stop and restart the command manually after changing
application source files.

## Existing local database

The viewer requires the existing local database:

```text
instance/
  messages.db
```

The application has no web action or command-line option to import message
files, rebuild the database, or recreate a missing database. Keep a private
backup of `instance/messages.db`. If the database is missing or corrupted,
stop the application and restore that file from your backup before starting it
again.

The database is local private data and should remain ignored by Git. The viewer
opens it read-only during normal requests. SQLite stores message text and safe
photo paths relative to `data/photos`; it does not store photo binaries.

## Local source data and photos

Keep the private export in this local layout:

```text
data/
  message_1.html
  message_2.html
  ...
  photos/
    <Instagram photo files>
```

The application does not edit, move, rename, or delete anything in `data/`.
The entire `data/` directory remains local and ignored by Git. Git ignoring a
file does not prevent Flask from reading it.

Do not move photos into the Git-tracked `static/` directory. The app serves
verified local images through its restricted `/photos/<filename>` route. The
route resolves only safe relative paths beneath `data/photos`, rejects paths
that could traverse outside that approved directory, and never fetches missing
photos from Instagram or another external source.

Each message's stored relative photo path connects that message to its matching
local file. Missing or invalid references appear as **Photo unavailable**.
Pages request photos only for the messages currently displayed, and each image
uses browser lazy loading, so the app does not scan or load the entire photos
folder at once.

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
report. Reports are saved locally in `reports/` and downloaded by the browser.
They contain inline CSS, no external scripts, and no tracking. Keep generated
reports local and ignored by Git because they can contain private message data.

The Instagram message timestamps in this database do not include timezone
information. The app preserves the original timestamp text and treats its
clock fields as a stable timezone-neutral ordering value.
