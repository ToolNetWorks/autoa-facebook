# Group publishing integration candidate

This is executable source on `integration/group-publishing-v20`, not a patch-only
handoff. The maintained runtime changes and all 29 reviewed staging files are in
the branch's normal source tree. Production is not deployed by this branch.

## Upstream behavior and actual callers

Definitions: INTEGRATED means upstream code/dependency directly incorporated;
ADAPTED means behavior implemented in ToolNet and reached from its runtime;
REFERENCE_ONLY means comparison without a new upstream implementation;
NOT_IMPLEMENTED includes deliberately excluded unsafe or unrelated mechanisms.
No upstream package qualifies as directly INTEGRATED. ToolNet's internal modules
below are connected; that is different from importing four external applications.

| Source | Original logic | ToolNet implementation and actual caller | Classification |
| --- | --- | --- | --- |
| ariknih `main.py:FacebookGroupSpam.post_to_groups` | Group navigation, open composer, fill caption, photo input/chooser | `app.execute_job` → `group_job_lifecycle.worker_payload` → CLI `post` → `fb_browser.attempt_post` → `group_links.normalize_group_url`, `group_access.inspect_group_access`, `composer_detector.find_composer/find_editor`, `media_upload.upload_media` | ADAPTED |
| ariknih `main.py` Publish selection | Last/first button selection, force click | `find_publish_button` → registry unique enabled match inside active composer → exactly one `submit.click` | Safe behavior ADAPTED; forced/first/last-button algorithm NOT_IMPLEMENTED |
| ariknih session/batch functions | Per-account browser state and bulk loop | `context_for` uses account profile lock; `close_context` calls `browser_session.atomic_storage_state` | Session comparison REFERENCE_ONLY; bulk loop NOT_IMPLEMENTED |
| tekdt `AutoPostFB.py:PostingThread.post_to_location` | Dialog scope, caption readback, bounded timeout | Registry/dialog detection; `attempt_post` reads full caption after upload and destination-lock processing, rechecks Group/image/identity/session before Submit | ADAPTED |
| tekdt `PostingThread.upload_images` | Hidden input availability | `media_upload.upload_media` resolves hidden input or explicit chooser, then checks attachment and progress | Hidden-input technique ADAPTED; upstream sleep does not count as upload verification |
| OpenCLI `src/composer.js:openComposer/insertCaption/findEnabledPostButton` | Composer/edit/Publish phases | `composer_detector` and pinned registry called by `attempt_post`; ambiguous controls stop safely | ADAPTED |
| OpenCLI `src/images.js:attachImages/waitForPreviews` | Wait for real preview | `media_upload.attachment_ready`, called after upload and again immediately before submission | ADAPTED; DataTransfer/Browser Bridge NOT_IMPLEMENTED |
| OpenCLI `src/auth-detection.js:classifySecurityState/assertSafePage` | Login/security stop | `facebook_session.inspect_session` from session check, Group access and final pre-submit check | ADAPTED; no checkpoint/CAPTCHA bypass |
| OpenCLI `group-post.js:groupPost` | Dry-run/evidence | CLI `preflight` forces `dry_run=True` → actual `attempt_post` → final identity/caption/image checks and screenshot; CLI `dry-run` remains payload validation only for compatibility | ADAPTED |
| OpenCLI `src/composer.js:pollPostOutcome`, `worker/posting-worker.js` | Submission notices and worker result | `post_verifier.wait_post_outcome` + `post_evidence.verify_post`; `execute_job` validates result before persistence/history | ADAPTED; composer-closed-as-POSTED NOT_IMPLEMENTED |
| Wahu `fb_group_poster.py:save_session_state`, retry and logging classes | Atomic state, bounded retry, structured events | New ToolNet implementations in `browser_session`, `retry_policy`, `logging`, called by `attempt_post`/`close_context` | REFERENCE_ONLY for upstream source; equivalent functionality implemented independently against existing contracts |

See [NOTICE](../NOTICE.md) for exact revisions and provenance limitations. No source
from the unlicensed Wahu repository is copied by this candidate.

## Runtime changes beyond the staging patch

- Registry `v2`: `desktop` and `desktop_semantic` resolution policies, vi/en,
  validation, ambiguity diagnostics and a ContextVar snapshot per workflow.
  Composer, upload, identity, session/access, post evidence and existing Reel
  controls use the registry. These are two desktop policies, not proof of support
  for two live Facebook layouts. No tenant-supplied selector paths or overrides.
- Post evidence requires a unique article with exact caption, matching Group/new
  post link, recent timestamp, identity and the actual attached photo. Avatar,
  nested/shared article, wrong photo and duplicate-caption ambiguity cannot prove
  POSTED. Redirect to another Group after Submit remains unverified.
- Session writes export complete state before replacing the last good file, use
  private permissions and fsync. JSONL logging appends instead of losing earlier
  events. Retry overrides cannot replay uncertain Submit/security failures.
- Attribution now keys Group content by `jobs.id`, not `posts.id`. Campaign creates
  attribution per Group execution; promotion/review/results pass the same ID and
  Pipeline. Account/tenant conflicts are refused without overwriting historical
  rows or rolling back a newly observed publication result.
- History maps each Auto Job to its same-Pipeline/Post/account/Group worker result.
  Analytics joins and snapshots are tenant/account checked. Dashboard errors are
  shown as errors, and Auto Jobs link to the actual Post. The confirmed-post KPI
  uses the same evidence-aware history query, including both posted/published
  legacy labels. Worker diagnostics are mapped to safe error codes/messages.
- Startup no longer deletes expired/revoked web-session history. Authentication
  still rejects those sessions. Retention deletion requires separate maintenance.
- Membership guards use existing schema and refuse ambiguous ownership. V20 is
  explicit only: no automatic migration, suspension or reassignment.

The single-image Group path is wired end-to-end. The auto workflow intentionally
creates Auto Job first, then Post + worker job at approved promotion; changing that
order would require a separate data-model change. Campaign API creates one Post
with separate jobs for multiple Groups. Both paths are tested.

## Validation and release limits

See [validation results](group-publishing-v20-validation.md); hashes are recorded
with the private release manifest. The new
E2E exercises real local API/SQLite, schedule/approve/promotion, claim, subprocess,
Chromium fixture, verifier, persisted history and authenticated local Dashboard.
All Facebook requests in that test are intercepted. Fixture permalinks are not live
Facebook posts. Responsive assertions cover 390/402/430/440/768/834/1280/1440.

There has been no live Submit in this task, no new live permalink, and no production
Admin login supplied for verification. Prior live preflight cannot certify the new
verifier, Nickname path, Reel selectors or the complete production worker.

Production data still has conflicting active CTV memberships. This candidate must
not be deployed until the owner decides the retained assignment, approves the
non-destructive reconciliation, and V20 is separately approved/applied. Historical
attribution inconsistencies require an approved targeted repair; this release does
not rewrite them. See the [release runbook](group-publishing-v20-runbook.md).
