# WeChat question dataset capture

> [!NOTE]
> Historical one-shot `dataset-sync` (`getmsglist`) plan completed on 2026-11-25 (`WECHAT_APP_ID` / `WECHAT_APP_SECRET` direct query). Live reader intake is now active via Cloud Hosting forwarding -> [relay_receiver.py](../relay_receiver.py) -> SQLite `inbound_events` ([STEP2](STEP2-FILTER-AND-MERGE-PIPELINE.md), [STEP3](STEP3-TIER1-AND-TIER2-RAG.md)).

## Context

The current service receives plaintext text messages at `/wechat`, but this data-collection step will run once as a standalone CLI against the official server-side `getmsglist` history API. That API returns customer-service records with `opercode=2003` for customer-received messages, permits at most a 24-hour time range and 10,000 records per request, and is not available through WeChat Cloud Hosting cloud calls. The CLI will fetch an explicit China-local date range, deduplicate inbound questions without retaining OpenID, and write a directly editable CSV plus a resumable local cursor. The main API keeps automatic RAG answers disabled during the real-account collection test. This touches seven tracked files and adds no database, scheduler, or runtime service.
## Approach

1. **Implement the direct `getmsglist` client.** Create `rag-poc/rag_poc/dataset_sync.py` because no existing package module can call this history API without importing the FastAPI application.

   - Add `DatasetSyncError(Exception)` with a stable `code` string, safe message, and `cause_type` for operator output.
   - Add frozen `HistoryPage(records: list[dict[str, object]], number: int, next_msgid: int)` and `WeChatHistoryClient.from_env(env: Mapping[str, str]) -> WeChatHistoryClient`. Require non-empty `WECHAT_APP_ID` and `WECHAT_APP_SECRET`. `WeChatHistoryClient(app_id: str, app_secret: str, *, timeout_seconds: float = 5.0)` exposes `fetch_page(*, start_time: int, end_time: int, msgid: int, number: int = 10000) -> HistoryPage`.
   - Fetch a direct access token from `https://api.weixin.qq.com/cgi-bin/token` with `grant_type=client_credential`, `appid`, and `secret`. Cache it for one CLI invocation and refresh it 60 seconds before `expires_in`. POST `{"starttime": ..., "endtime": ..., "msgid": ..., "number": 10000}` to `https://api.weixin.qq.com/customservice/msgrecord/getmsglist?access_token=...` using the standard-library `urllib.request` JSON pattern already present in `app.py`. The client uses direct HTTPS because the official documentation says `getmsglist` does not support cloud calls.
   - Validate that `recordlist` is a list, `number` is a nonnegative integer, and `msgid` is a positive integer when the returned page is full. Use `len(recordlist)` as the page-size signal. Never compare `number` with `len(recordlist)` and never use `number` to decide whether another page exists, because the API response’s `number` field is not the page length. A nonzero WeChat `errcode`, invalid JSON shape, or network failure raises `DatasetSyncError` without logging the token, response body, OpenID, or message text.
   - If a page request returns `errcode=40001`, clear the cached token, fetch one new token, and retry that same page once. Other API errors fail the invocation without changing the CSV or cursor.

2. **Define the local CSV and cursor contract.** Add these symbols to `rag-poc/rag_poc/dataset_sync.py` because no existing project component persists history records.

   - Add frozen `HistoryQuestion(record_key: str, text: str, occurred_at_utc: datetime)`, `SyncCursor(range_start_date: date, range_end_date: date, next_date: date, next_msgid: int)`, and `SyncStats(pages_fetched: int, inbound_records: int, inserted_rows: int)`.
   - Add `CsvDatasetStore(output_dir: Path)` with `initialize() -> None`, `locked() -> Iterator[None]`, `load_cursor(*, range_start_date: date, range_end_date: date) -> SyncCursor | None`, and `append_page(*, questions: Sequence[HistoryQuestion], next_cursor: SyncCursor) -> int`. Store exactly three files below `output_dir` named `questions.csv`, `sync-state.json`, and `.sync.lock`.
   - `initialize()` creates `output_dir` with mode `0700`, creates `questions.csv` with mode `0600` and the header `record_key,message_time_utc,question,source`, and leaves the state file absent until the first successful page. Write CSV as UTF-8 with BOM, RFC CSV quoting, and `newline=""` so Chinese text opens correctly in spreadsheet tools. Every row uses the literal source value `getmsglist`.
   - `locked()` takes an exclusive `fcntl.flock` on `.sync.lock` for the whole sync invocation and releases it in `finally`. A second collector invocation exits with a safe `dataset_lock_unavailable` error instead of interleaving writes.
   - Before reading existing rows, repair only an incomplete final physical CSV line by truncating to the previous newline. Reject a wrong header or any malformed earlier row without contacting WeChat. Load existing `record_key` values into a set. `append_page` writes only unseen keys, flushes and `fsync`s the CSV, then atomically writes `sync-state.json` through a same-directory temporary file and `os.replace`. A crash after the CSV write and before the state replacement causes a safe re-fetch and deduplication. A crash after the state replacement cannot lose a row because the CSV is durable first.
   - `sync-state.json` contains only `range_start_date`, `range_end_date`, `next_date`, `next_msgid`, and `updated_at_utc`. It contains no credentials, OpenID, question text, or hash key.
   - Add `make_record_key(record: Mapping[str, object], hash_key: str) -> str`. Canonicalize exactly `openid`, `opercode`, `text`, `time`, and `worker` with sorted compact JSON, using an empty string when `worker` is absent, and compute HMAC-SHA256 with `WECHAT_DATASET_HASH_KEY`. Keep OpenID in memory only while computing the key. Store the 64-character digest in CSV for deduplication and omit it from cleaned copies.

3. **Implement one-shot, China-day-aligned synchronization.** Add `sync_history(client: WeChatHistoryClient, store: CsvDatasetStore, *, start_date: date, end_date: date, reset: bool = False) -> SyncStats` in `dataset_sync.py`.

   - Use `ZoneInfo("Asia/Shanghai")` for all date boundaries. Require `start_date <= end_date` and reject an `end_date` later than the current China-local date. With no existing cursor, start at `(start_date, 1)`. With an existing cursor for the same range, resume it. If the existing range differs, require `reset=True`, then restart at `(start_date, 1)` while preserving the current CSV so HMAC deduplication makes the backfill safe.
   - For each `next_date`, set `day_start` to China-local midnight. For a completed day, set `day_end` to one second before the following China-local midnight. For the current China-local day, set `day_end` to the current time and leave the cursor on that day with `next_msgid=1` after a short page so a one-shot run can be rerun later without skipping messages that arrive after the run. Every request therefore stays within one local calendar day and below 24 hours. Never use fixed 23-hour windows.
   - Request each day with `starttime=int(day_start.timestamp())`, `endtime=int(day_end.timestamp())`, `msgid=next_msgid`, and `number=10000`. When `len(recordlist) == 10000`, require a changed positive response `msgid` and request the next page for the same day. When `len(recordlist) < 10000`, advance to the next date with `msgid=1` for a completed day, or commit the current-day cursor and finish the invocation. The response `number` value is recorded only for diagnostics and never controls pagination.
   - Convert only records with integer `opercode == 2003` into `HistoryQuestion` values. Require a non-empty string `openid`, a string `text`, and a nonnegative integer `time` for each inbound record. Convert `time` from Unix seconds to UTC and preserve `[image]`, `[voice]`, and `[video]` markers. Ignore `opercode == 2002` staff-sent records because this dataset contains questions only. Any malformed inbound record fails the page before the CSV or cursor changes.
   - Compute HMAC keys, validate the complete page, call `append_page` with the next cursor, and continue until the requested range is complete or the current day is reached. API, validation, or file failures leave the last committed cursor in place so rerunning the same command resumes safely. Log only page and inserted-row counts plus stable error codes.

4. **Keep automatic answers disabled in the main API.** Update `app.py` independently of the collector.

   - Add `_AUTO_REPLY_ENABLED = _env_flag("WECHAT_AUTO_REPLY_ENABLED", True)`. The default preserves the existing answer path for deployments that do not opt into collection-only mode.
   - In `_lifespan`, initialize `application.state.rag_responder` to `None`. When `_AUTO_REPLY_ENABLED` is true, run the existing `RagResponder()` initialization. When it is false, skip the RAG import and initialization entirely so the main API makes no provider request.
   - When `_AUTO_REPLY_ENABLED` is false, `receive_callback` returns HTTP 200 with `success` for valid cloud or signed callbacks without calling `responder.answer`, scheduling `_answer_then_send`, or calling `_send_customer_message`. `debug_rag_query` returns HTTP 404 even if `RAG_DEBUG_TOKEN` is set. Do not add callback persistence because the standalone CLI is the only collection source.
   - When `_AUTO_REPLY_ENABLED` is true, preserve the current callback, RAG, debug, dashboard, and outbound behavior. Re-enable automatic answers by setting the flag to `true` after the CSV collection test.

5. **Expose one private CLI command.** Extend `rag-poc/rag_poc/cli.py` using the existing `argparse` and safe error conventions.

   - Add `dataset-sync` with required `--start-date YYYY-MM-DD`, required `--end-date YYYY-MM-DD`, required `--output-dir PATH`, and optional `--reset`. Parse dates as China-local calendar dates. Construct `CsvDatasetStore(Path(args.output_dir))`, `WeChatHistoryClient.from_env(os.environ)`, and call `sync_history`. `--reset` is required only when an existing cursor covers a different date range. The command creates the CSV directory and header automatically.
   - Load the existing `rag-poc/.env` and parent project `.env` conventions before dataset configuration so current `WECHAT_APP_ID` and `WECHAT_APP_SECRET` values can be reused. Require `WECHAT_DATASET_HASH_KEY` from the environment and never print it.
   - Write progress and errors to stderr only. The collection artifact is `questions.csv`; there is no JSONL export command, database migration, recurring scheduler, or public HTTP endpoint. The command does not initialize RAG, call a model provider, or send customer messages.
   - Keep `check-config`, `index`, and `query` unchanged.

6. **Document one-shot server usage and cleaning.** Update `README.md` and `rag-poc/README.md` with the exact run contract.

   - Run the CLI on any server whose outbound IP is allowlisted for the official-account API. Set `WECHAT_APP_ID`, `WECHAT_APP_SECRET`, and a stable generated `WECHAT_DATASET_HASH_KEY` in a protected shell environment or an external env file. Do not put them in the repository, command history, CSV, or logs.
   - Run a 30-day initial collection with the China-local date range computed by the shell and an output directory outside the repository.

     ```bash
     DATASET_START_DATE="$(TZ=Asia/Shanghai date -d '30 days ago' '+%F')"
     DATASET_END_DATE="$(TZ=Asia/Shanghai date '+%F')"
     /srv/wechat-dev-test/.venv/bin/python -m rag_poc.cli dataset-sync \
       --start-date "$DATASET_START_DATE" \
       --end-date "$DATASET_END_DATE" \
       --output-dir /srv/wechat-dataset
     ```

   - Rerun the same command after a failed or interrupted run to resume its cursor. Use `--reset` with a new explicit date range to backfill another period while preserving existing rows. Copy `/srv/wechat-dataset/questions.csv` to a separate cleaning file before editing it. Keep the canonical CSV and `sync-state.json` unchanged for future deduplication and reruns.
   - During the real-account test, set `WECHAT_AUTO_REPLY_ENABLED=false` in the main API environment and keep public access disabled. After collection, set it to `true` only when the existing RAG path should resume.
## Critical files & anchors

- `app.py` at `_lifespan`, `debug_rag_query`, and `receive_callback` around lines 48–63, 541–588, and 600–645. These are the only main-service changes and must enforce the temporary auto-reply-off mode.
- `rag-poc/rag_poc/dataset_sync.py` as a new module. It owns direct token/API calls, China-local day windows, `len(recordlist)` pagination, HMAC keys, CSV durability, and cursor resumption.
- `rag-poc/rag_poc/cli.py` at `create_parser()` and `main()` around lines 721–783. Add the one `dataset-sync` command without routing it through RAG setup.
- `README.md` and `rag-poc/README.md`. Document the standalone server command, protected environment variables, output files, and cleaning workflow.
- `tests/test_app.py` and `rag-poc/tests/test_dataset_sync.py`. Prove collection-only ingress, API response validation, China-day boundaries, full-page pagination, CSV deduplication, crash-safe cursor order, and malformed-input failures.
## Verification

1. From the repository root, run `./.venv/bin/python -m unittest discover -s tests -v`. The existing callback suite and new auto-reply-off tests must pass with `WECHAT_AUTO_REPLY_ENABLED` defaulting to true.
2. From `rag-poc/`, run `./.venv/bin/python -m unittest discover -s tests -v`. The history client, CSV store, and CLI path must import without live WeChat, RAG-provider, or filesystem data-directory access outside temporary test directories.
3. Test API response handling with a fake client response whose `number` is `10000` and whose `recordlist` contains one inbound record. Assert the page is accepted and treated as complete because `len(recordlist) == 1`. Then return exactly 10,000 records with a changed `msgid` and assert the next request uses that cursor. Assert no implementation path compares `number` with `len(recordlist)`.
4. Test date boundaries with `ZoneInfo("Asia/Shanghai")`. For a completed day, assert the request starts at local `00:00:00` and ends at `23:59:59` local, and the next request starts at the following local midnight. Assert no request crosses a China-local calendar day. Test the current-day short-page path and assert the cursor remains on the current date with `next_msgid=1`.
5. Test CSV durability with a temporary output directory. Assert the exact header, UTF-8 BOM, RFC quoting for a question containing commas and newlines, HMAC deduplication across repeated pages, `0600` files, and `0700` directory permissions. Simulate a state-write failure after a durable CSV write, rerun the same page, and assert no duplicate row and a committed cursor after the retry. Test an incomplete final line repair, wrong headers, malformed earlier rows, and concurrent lock acquisition.
6. Exercise collection-only ingress with `_AUTO_REPLY_ENABLED=false` using a valid cloud text callback containing `Content=开放时间？` and `MsgId=dataset-1`. Assert HTTP `200 success`, no `RagResponder` construction, no responder call, no background task, and no `_send_customer_message` call. Exercise `debug_rag_query` with a token and assert HTTP 404. Then run the existing `_AUTO_REPLY_ENABLED=true` callback tests and assert the prior RAG path remains available.
7. On the chosen server, set protected `WECHAT_APP_ID`, `WECHAT_APP_SECRET`, and `WECHAT_DATASET_HASH_KEY`. Run `dataset-sync` for a range containing a known inbound private message that received a reply. Use Python’s `csv.DictReader` to verify the exact CSV header `record_key,message_time_utc,question,source` and the expected inbound message. Check that `sync-state.json` records the next China-local date and page cursor. Repeat the run against the same completed range and verify the row count is unchanged.
8. **Superseded live check.** The account administrator confirmed that this account’s private messages appear in `getmsglist` only after receiving a reply. An unanswered message is therefore expected to be absent from the CSV. Verify collection-only callback behavior separately with an authorized test message while `WECHAT_AUTO_REPLY_ENABLED=false`. Confirm the callback is acknowledged and the sender receives no automatic reply. Edit a copy of the CSV to verify the cleaning workflow. The canonical CSV stays under the collector’s control. Set `WECHAT_AUTO_REPLY_ENABLED=true` only when resuming the RAG reply path. [Live evidence and collector checkpoint](README.md#微信历史问题采集一次性-cli)
## Assumptions & contingencies

- The live official-account deployment uses `WECHAT_AUTO_REPLY_ENABLED=false`. The service must not initialize RAG or call any model, embedding, or customer-message API while this flag is false. Setting it to true is the explicit switch back to the existing answer path.
- The standalone server has outbound HTTPS access and an IP allowlisted for the official-account API. If WeChat returns `40164`, add that server’s egress IP to the official-account allowlist or run the same CLI on a whitelisted server. No database or public endpoint is added as a workaround.
- The account can call the authenticated customer-service history API. If WeChat returns `65400`, enable or upgrade the account’s customer-service capability before collecting. If it returns `65416` or `65417`, the command reports the error and leaves the CSV and cursor unchanged.
- The API does not document a message-history retention period in the project sources. The CLI collects only the explicit `--start-date` through `--end-date` range that WeChat returns.
- `WECHAT_DATASET_HASH_KEY` remains unchanged for the lifetime of the canonical CSV. Rotating it changes record keys and can reinsert old messages during `--reset`, so a deliberate new dataset must use a new output directory.
- The canonical `questions.csv` and `sync-state.json` remain under the collector’s control. Cleaning uses a copied CSV, preserving the source rows for safe reruns and deduplication.
- No automatic retention or recurring collection is added. The one-shot command ends after the requested range or current-day page is committed, and the operator controls when to run it again.
