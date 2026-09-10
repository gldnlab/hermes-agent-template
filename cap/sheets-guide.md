## Cap's authenticated Google Sheets access (v1)

Private client source spreadsheets are accessible through the installed helper:

    python3 /app/cap/sheets.py metadata 'SPREADSHEET_ID_OR_URL'
    python3 /app/cap/sheets.py read 'SPREADSHEET_ID_OR_URL' "'Firm Financials'!A1:E5"
    python3 /app/cap/sheets.py read 'SPREADSHEET_ID_OR_URL' "'Firm Financials'!A1:E5" --render FORMULA

Use metadata to discover exact tab names. Read only ranges needed for the task.
UNFORMATTED_VALUE returns dates as serial numbers; FORMATTED_VALUE is the default.
This helper authenticates as the existing client-data service account and only
requests the Sheets read-only scope. It does not use your ChatGPT login or the
public export URL. A failed public fetch is not evidence this connection failed.

Use this helper from Codex's command tool when checking source data. Do not read,
print, copy, commit, or change its credentials. Never add credentials to a repo,
worktree, task comment, prompt, or Slack message. It emits actionable errors and
a matching error ID on stderr; report that ID and blocker if authentication fails.
Do not change client source sheets under this workflow. No Sheets write operation
is provided. The helper's read-only scope is not a container security boundary:
the shared service account may have wider permissions outside this helper.
