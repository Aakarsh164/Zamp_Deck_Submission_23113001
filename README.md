# Forward-to-Decide

An AP inbox that starts at zero trust and earns autonomy one approval at a time. Forward a vendor invoice, watch each stage run, and get a decision with the reasons and what would change it.

## How it works (process map)

1. **Ingest**: email webhook or upload. Store hash, sender, source.
2. **Extract**: the configured extractor reads the PDF or image into JSON. Missing or invalid fields are null, never guessed. Each critical field has a confidence and an evidence snippet.
3. **Match vendor**: fuzzy match to the vendor master. No match is a hard stop.
4. **Match PO**: explicit reference, or implied by vendor plus open PO plus amount fit. Ambiguous means a human looks.
5. **Rules**: deterministic code, no LLM (below).
6. **Decide**: fraud/ambiguous hard stops escalate; confirmed exact duplicates are auto-rejected; missing fields trigger a vendor-email draft; delivery occurs only when SMTP is configured and the recipient is allowlisted; other failures and uncertain cases remain for review; only clean trusted invoices can auto-approve.
7. **Human loop**: approve or reject. Agreeing with a recommended approval gives +1 trust. Overriding the system costs -3.
8. **Learn**: repeated overrides produce a rule proposal.

The LLM only reads documents. Every decision comes from rules in `rules.py`, and the explanation is built from the rules that fired, so it cannot contradict the decision.

## Rules

| Rule | Check | On failure |
|---|---|---|
| R1 | Vendor, invoice number, date, total present | Hold and draft a request for the missing fields; SMTP delivery requires explicit configuration and allowlisting |
| R2 | Vendor in approved master and active | Hard stop |
| R3 | PO exists, open, belongs to this vendor | Review (wrong-vendor PO is a hard stop) |
| R4 | Total vs PO **remaining balance** within vendor tolerance | Warn inside tolerance, fail outside |
| R5 | Exact duplicate (same file hash, or same vendor, normalized invoice number and amount), near-duplicate (same vendor, amount within 0.5%, within 30 days, number differs by 2 characters or fewer), or likely corrected resubmission (same vendor/PO, close number/date, total changes by at most 20%) in 90 days | Confirmed duplicate: auto-reject; suspected fraud/resubmission: escalate; non-conclusive PO failures: review |
| R6 | Bank account matches the vendor master | Hard stop |
| R7 | Lines, subtotal, tax and total reconcile | Review |

Fraud indicators, changed bank details, near-duplicates, a repeated invoice number with a changed/unknown amount, and ambiguous PO matches escalate regardless of trust. A confirmed exact duplicate is auto-rejected. Non-conclusive rule failures (such as an over-PO invoice) remain in human review. Hard stops can never auto-approve.

## Trust model

Every vendor starts at 0 points and all invoices need a human. At 5 points, a vendor's clean invoices auto-approve. That requires no warnings, a total at or below $10,000, and extraction/match confidence of at least 0.90. Confidence from 0.70 to 0.90 requires additional verification; below 0.70 requires human verification. Auto-approvals do not add points. Near-misses (inside tolerance) still go to a human. Thresholds live in `data.py`.

## Decision output

`action` (AUTO_APPROVE, AUTO_REJECT, REVIEW, HOLD_FOR_INFO, ESCALATE), `decision_explanation`, `recommendation`, `confidence` (the lower of extraction confidence and PO-match confidence), `vendor`, `po` (amount, invoiced_before, remaining, method), `invoice` fields, `checks` (rule, status, detail), `reasons`, `hard_stops`, `what_would_change_it`, and `vendor_email` (message, recipient, status, and any error). Email statuses include `OUTBOX`, `PENDING`, `SENT`, and `FAILED`; attempts and their outcomes are persisted in SQLite.

## Edge cases (my four)

| Edge case | Sample button / Gauntlet case | Behaviour |
|---|---|---|
| Split PO | "Split PO", "Over PO balance", Gauntlet 5 | Running balance per PO. Flags when cumulative invoices exceed PO plus tolerance |
| Near-duplicate | "Near-duplicate" (run Clean first) | Hard stop even though the invoice number differs |
| Changed bank details | "Changed bank details", Gauntlet 8 | Hard stop, even for a trusted vendor |
| Missing critical field | "Missing invoice number and total" | Holds, does not guess, drafts a request email; SMTP delivery is opt-in and allowlisted |

## Run locally

```
python -m venv .venv
.venv\Scripts\activate          # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # add a FREE GEMINI_API_KEY (samples work without any key)
uvicorn main:app --reload       # http://127.0.0.1:8000
```

Run the unit, email, analytics, and decision-rule tests with:

```
python -m unittest discover -s tests -v
```

The 16-scenario decision Gauntlet is available from the UI or with:

```
python -c "import gauntlet; print(gauntlet.run())"
```

Sample invoices and the Gauntlet need no key. For real uploads, get a free key at https://aistudio.google.com/apikey and set `GEMINI_API_KEY` (reads PDFs, scans and photos). With no key the app falls back to a local text-PDF reader. `EXTRACTOR` can select `gemini`, `groq`, `anthropic`, or `local`; if a provider fails, the app tries the next configured provider and then the local reader for PDFs. Invalid JSON is retried once, each provider attempt is limited by `EXTRACT_TIMEOUT` (90 seconds by default), and the Extract stage shows which provider succeeded. Numeric fields and dates are normalized; invalid values become null. A paid `ANTHROPIC_API_KEY` also works. Use only fake invoices on the free tier, because Google may use free-tier content to improve its products. Run `python check_key.py` to confirm.

### Vendor email requests

`EMAIL_MODE` defaults to `demo`. For `HOLD_FOR_INFO` invoices, demo mode records the drafted request as `OUTBOX` with the message body in Email History; it never contacts SMTP. Missing vendor contacts remain visible as drafts, not delivery failures.

To enable outbound SMTP, set `EMAIL_MODE=smtp`, configure SMTP, and explicitly allow recipients with `EMAIL_ALLOWLIST` (comma-separated full email addresses). An empty allowlist sends nothing. SMTP sends only when configuration is complete and the recipient is allowlisted; otherwise the request stays in the outbox with a reason. Configure SMTP in the untracked local `.env` file:

```
EMAIL_MODE=smtp
EMAIL_ALLOWLIST=authorized-recipient@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM_EMAIL=accounts@your-company.com
```

SMTP uses STARTTLS. Set both `SMTP_USER` and `SMTP_PASSWORD` when authentication is required. `SMTP_FROM` and `SMTP_USERNAME` are accepted as aliases for older local configurations. Vendor contacts can alternatively be bootstrapped with a JSON `VENDOR_EMAILS` mapping in `.env`; the Vendors tab lets you maintain them in the database. Use only authorized contact addresses. Email statuses are `SENT`, `FAILED`, `PENDING`, `OUTBOX`, and `NOT_REQUIRED`; failed attempts include a reason and can be retried from Email History or invoice details. Notification history is append-only across retries, and reprocessing the same missing-information request does not send it again. `SENT` means the SMTP server accepted the message, not that it reached the vendor's inbox; delivery/bounce confirmation requires provider-side tracking. SMTP credentials are server-side only and are not returned by the API.

### Public-demo safeguards

Invoice uploads are limited to 5 MB and PDF, PNG, JPG, or WebP. The upload endpoint allows 10 requests per hour per client IP in memory and returns HTTP 429 when the limit is exceeded. Set `ADMIN_TOKEN` to protect reset, tolerance-proposal acceptance, and vendor-email updates; send it in the `X-Admin-Token` header or `admin_token` query parameter. When unset, these controls remain available locally. Run `python check_secrets.py` before publishing; it scans tracked files (or source files when the folder is not a Git checkout) for likely credentials without printing matched values. Keep `.env` out of source control.

### Analytics dashboard

The dark dashboard has Overview, Invoices, Vendors, Email History, and Analytics sections. Date, vendor, decision, invoice-status, and email-status filters apply to the metrics and charts. Analytics are calculated from non-seeded invoice runs in SQLite; the seeded demo history is excluded, so metrics start at zero and change as real/sample invoices are processed. Charts use native HTML/SVG and require no chart-library install. Selecting a recent invoice opens its decision explanation and recorded email attempts.

The relevant APIs are:

- `GET /healthz` — readiness check; verifies the app can query its SQLite database.
- `GET /api/samples` — sample buttons and labels; `POST /api/sample/{key}` — run one sample without extraction.
- `POST /api/run` — upload a PDF/image as multipart field `file`; returns newline-delimited pipeline events.
- `POST /api/gauntlet` — evaluate the current rule set against the hostile fixture cases and return score and misses.
- `POST /api/reset` — reset demo data.
- `GET /api/analytics` — filtered KPIs, decision counts, trends, vendor performance, monthly value, and email delivery counts.
- `GET /api/runs` — filtered invoice activity; `GET /api/runs/{run_id}` — decision and email details.
- `POST /api/runs/{run_id}/review` — record a human approve/reject verdict.
- `POST /api/runs/{run_id}/resubmit` — process a corrected PDF/image for a pending `HOLD_FOR_INFO` run; the original is marked superseded and linked to the new run.
- `GET /api/email-history` — notification attempts, recipients, timestamps, and failure reasons.
- `POST /api/runs/{run_id}/emails/{notification_id}/resend` — retry a failed attempt.
- `PUT /api/vendors/{vendor_id}/email` — update a vendor contact.
- `GET /api/proposals` — tolerance proposals with a replay of that vendor's non-seeded runs: changed decisions, additional auto-approval eligibility, and later human rejections among those invoices.
- `POST /api/proposals/accept` — accept a tolerance proposal.
- `POST /api/inbound/{token}` — receive an authenticated forwarded-invoice payload; `/api/live` exposes the processing events.

Pass any supported filters as query parameters: `date_from`, `date_to` (`YYYY-MM-DD`), `vendor_id`, `action`, `status`, and `email_status`.

The backtest replays decisions with the recorded trust score and invoice evidence. A tolerance warning still requires human review, so increasing tolerance alone does not make an over-PO invoice eligible for auto-approval.

## Email ingestion

`POST /api/inbound/<INBOUND_TOKEN>` accepts Postmark-style inbound JSON (`From`, `Attachments[{Name, ContentType, Content(base64)}]`). Point your inbound provider's webhook at it. The token in the URL stops strangers triggering runs. The UI polls `/api/live`, so forwarded invoices appear in the stage view with no page action. Test without any provider:

```
curl -X POST http://127.0.0.1:8000/api/inbound/dev-token -H "Content-Type: application/json" -d "{\"From\":\"v@x.com\",\"Attachments\":[{\"Name\":\"a.pdf\",\"ContentType\":\"application/pdf\",\"Content\":\"<base64>\"}]}"
```

Other providers (Mailgun routes, a Cloudflare Email Worker) can post the same JSON shape.

For inbound invoices, R9 compares the sender domain with each vendor's `domains` list in `data.py`. Exact matches pass; mismatches and close lookalikes hard-stop to `ESCALATE`, with a reason and required verification. The configured domains are `.example.com` placeholders—replace them with independently verified vendor domains before use. Uploaded files have no sender and show a neutral "not applicable" check.

## Deploy on Render

1. Push the project to GitHub; `.env`, `app.db`, `.venv/`, and generated test invoices are excluded from source control and the Docker build context.
2. In Render, create a **Web Service** from the repository and select **Docker** as the runtime. Set the health-check path to `/healthz`; the Docker command uses Render's `$PORT` (falling back to 8000 locally).
3. Configure these environment variables in Render's dashboard: `EXTRACTOR=gemini`, `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-flash`, `INBOUND_TOKEN` (a unique secret), `ADMIN_TOKEN` (a unique secret), `EMAIL_MODE=demo`, and `AUDIT_EVERY=0` while recording the demo. To use Groq instead, set `EXTRACTOR=groq` and `GROQ_API_KEY`. Keep SMTP disabled for the public demo.
4. After deploy, run `python smoke_test.py https://<your-render-service>.onrender.com`. It checks the health endpoint, home page, samples, Gauntlet, and clean sample path. If an extractor API key is configured in the environment running the script, it also uploads a generated test PDF and checks the extractor stage.

The runtime dependencies in `requirements.txt` are pinned. SQLite storage on Render's free tier is ephemeral: invoice runs and local settings can be lost on restart or redeploy. Use a persistent disk or an external database for anything beyond a disposable demo; the simulated history regenerates on startup.

## De-risking checklist

- Run 3 real PDFs and 1 phone photo through extraction before recording anything.
- Keep a sample-invoice run ready as a fallback if the API key or network fails.
- Hit the deployed URL 10 minutes before the interview, because free tiers sleep.
- Test the email path from your own address the day before. Keep upload as the fallback.
- Click Reset demo data before each take so trust starts at zero.
- Never put a real vendor invoice or real bank details in the public demo.

## Five-minute demo outline (say it in your own words)

1. **0:00** One sentence: the inbox starts at zero trust and earns autonomy. Point out that the analytics dashboard shows actual runs, while any seeded demo data is excluded from its metrics.
2. **0:30** Happy path: run the clean invoice (forward it by email if the path works). Narrate each stage, then the reasons and "what would change it". Approve it and show trust 1/5.
3. **2:00** Trust moment: show a vendor at 5/5 auto-approving a clean invoice, then the same vendor with a changed bank account escalating anyway.
4. **3:00** Edge case: the split PO overrunning the balance, or the near-duplicate. Explain why the number differs by one character but is still caught.
5. **4:00** Gauntlet: run it and read the scorecard, including the suspicious corrected resubmission that is sent to a human for investigation.

## Questions to be ready for

- **Why is the LLM not deciding?** Finance needs repeatable, auditable decisions. Code decides, and the LLM only reads documents.
- **Why does trust drop 3 and rise 1?** Trust should be hard to earn and easy to lose. The numbers are policy knobs in `data.py`.
- **What stops a bad vendor gaining trust and then committing fraud?** Hard stops ignore trust. Bank changes, duplicates and unknown vendors always escalate.
- **Why 0.5% for near-duplicates?** It catches re-sent invoices with typos. The Gauntlet shows its blind spot.
- **How would you scale it?** Postgres instead of SQLite, a queue for extraction, a real vendor master and PO feed (ERP sync), per-field human correction, and an audit sample of auto-approvals.

## Known limitations

- Mock vendor and PO data. No login. Money is stored as floats.
- Audit sampling: 1 in 10 auto-approvals goes to a human, chosen by the file hash so the same invoice always behaves the same. Set `AUDIT_EVERY=0` to turn it off for demos.
- Scanned and phone-photo invoices use the same vision extraction. `python make_test_pdfs.py` also makes a scanned-style PNG for testing this path. The local fallback cannot read images.
- Gauntlet invoices are structured fixtures (no LLM) so it is fast and repeatable. It tests the rules, not extraction.
- Vendor matching is fuzzy and has no independent identity proof; a similar vendor name with the legitimate vendor's bank last-four can still be accepted for uploads, which have no sender-domain evidence. Inbound email checks require independently verified domains to be configured for each vendor.
- The sender-domain check trusts the inbound provider's `From` field; it does not itself verify SPF, DKIM, or DMARC. Protect the webhook and use provider-authenticated sender data.
- Rules validate line-item arithmetic but do not compare a unit price with vendor/item price history, so a mathematically consistent overcharge can pass.
- The PO master does not record PO creation dates, and rules do not reject implausibly old or future invoice dates.
