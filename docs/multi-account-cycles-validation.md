# Multi-account cycles acceptance — 2026-10-28

Scope: candidate branch `feature/multi-account-cycles`, based on V20 PR #1.
Changes are in an isolated Git worktree, not the running production checkout.

## Executed verification

| Check | Result | Evidence |
| --- | --- | --- |
| V20 regression, 31 Python modules | 455 PASS; 0 failures/errors/skips; 403.50s | `/tmp/toolnet-multi-account-regression.xml` and `.log` |
| New cycle acceptance, six Python modules | 43 PASS; 0 failures/errors/skips; 83.84s | `/tmp/toolnet-cycle-acceptance-final.xml` and `.log` |
| Affected-module regression after evidence/session fixes, seven modules | 145 PASS; 0 failures/errors/skips; 143.41s | `/tmp/toolnet-cycle-refresh-regression.xml` and `.log` |
| Distinct cases | **498 PASS** | Suites cover distinct test functions |
| Frontend typecheck | PASS | `npm run typecheck` |
| Frontend production build | PASS | `npm run build -- --outDir /tmp/toolnet-multi-account-frontend` |
| Responsive browser checks | PASS | Four cycle screens at 390, 402, 430, 440, 768, 834, 1280, 1440px |
| Whitespace | PASS | `git diff --check`; staged check also required before commit |
| Production source preservation | PASS | 219 baseline files have unchanged SHA256 |
| Runtime artifact/secret scan | PASS within scanned changes | No DB/media/profile/session artifacts or high-confidence secret patterns |

Regression module list and offline setup are recorded in
`group-publishing-v20-validation.md`. The extra DOM case verifies that a generic
Facebook account control is not mistaken for the person's display name.

New acceptance reproduction, from the candidate worktree only:

```sh
DATA_DIR=/tmp/toolnet-cycle-acceptance-final PYTHONPATH=.:tests/helpers \
  /root/facebook-pipeline-manager/.venv/bin/python -m pytest -p offline_runtime \
  tests/test_auto_cycles.py tests/test_auto_cycles_runtime.py \
  tests/test_auto_cycle_clock.py tests/test_auto_cycles_migration.py \
  tests/test_auto_cycles_e2e.py tests/test_auto_cycles_refresh.py \
  -q --disable-warnings --maxfail=3 --tb=short \
  -o faulthandler_timeout=60 \
  --junitxml=/tmp/toolnet-cycle-acceptance-final.xml
```

## Acceptance coverage

- 10 accounts / 50 Groups: exactly one persisted target per Group, balanced
  randomized selection, repeated timer/manual request does not duplicate work.
- 10 accounts / 50 Groups / 5 DISABLED: only 45 destinations get targets; disabled
  state survives account-local observations. No future job is created for them.
- Only two eligible accounts among ten can receive more than five assignments each
  when Admin quotas permit. No fixed per-account partition is imposed.
- Expired session, absent joined membership, per-account access failure and no
  eligible account affect their own destinations, not the whole cycle.
- DISABLED at the beginning, middle or end of queued work is skipped; other
  accounts/Groups continue. A later successful check plus Admin confirmation is
  required to reactivate; setting UNKNOWN cannot bypass the latch.
- Multiple recurring timer ticks/days, overnight windows and timezone arithmetic;
  no replay of uncertain pending submissions or all missed cycles.
- Concurrent timer/claims, account/Reel busy guard, effective account/Pipeline/
  Group quotas, and cooldown recheck after planning.
- Actual child process claims a job then exits abruptly. Isolated app startup
  recovers to NEEDS_REVIEW, reconciles Auto Job history, and another account runs.
- Content preparation lease recovery without duplicate Auto Jobs; frozen caption
  and media mapping cannot be edited into a different execution.
- AI failure, explicitly approved fallback, Pipeline/Group AI context and unique
  caption enforcement; no silent regeneration or unapproved fallback.
- Album scope, exhaustion, repeated image across different Groups and protection
  against deleting referenced media. CHECK_FAILED cannot become eligible merely
  through a usable flag.
- Anonymous unavailable and expired-session local browser outcomes stop before
  Submit. Pending approval and unverified Submit remain distinct; no requeue.
- Wrong-Group permalink does not count as POSTED. Existing verifier regression
  covers avatars, unrelated images, repeated captions and pending approval.
- Two independent Pipelines: scoped API access, catalog ownership, foreign keys,
  scheduling and outcome attribution. Duplicate UID import is rejected before
  starting a browser.
- V21 rollback/idempotency, unchanged historical rows, invalid parent FK and
  incompatible pre-existing index-name rejection.
- Legacy shared/CTV/distribute APIs cannot create parallel automatic work once a
  cycle configuration exists, even when that configuration is paused.
- Automatic Group-probe warmup and an overdue second-day cycle after session/pair
  evidence exceeds 24 hours. Existing probes refresh only matching identities;
  checkpoint accounts and disabled Groups are excluded, preparation is bounded,
  and probe recovery never retries Submit. A mismatched profile cannot mark the
  account connected or create false membership evidence. Proof omits raw UID.
- Post-outcome refresh retains anonymous/nickname support and the original check
  timestamp, avoiding immediate loss of eligibility or false freshness.

## End-to-end boundary and screenshots

The local test invokes the existing `prepare_auto_post_jobs` timer entry, ordinary
Auto Jobs, promotion into Posts/jobs, `claim_next_job`, real `execute_job` and a real
subprocess. Only the Facebook integration boundary is replaced with local Chromium
DOM fixtures. The same registry/composer/upload/identity/verifier code runs there.
Three different account/Group targets yield one verified fixture publication, one
pending approval and one unverified submission, with exactly one Submit click each.
Database, History, report API and an authenticated local Dashboard are then checked.

Screenshots stay outside Git under `/tmp/toolnet-remaining-ui/`:
`multi-account-config-*`, `multi-account-groups-*`, `multi-account-library-*`,
`multi-account-report-*`. The 390px configuration/Groups screenshots were also
visually inspected. The UI has two-row mobile navigation and no horizontal overflow
at any required width. API 500 returns a visible safe error instead of fake zeros or
private diagnostics. These are synthetic local credentials/data, not production
Admin authentication or Facebook publication evidence.

Earlier attempts exposed fixture schema/DB-path/unique-key mistakes and a real
frontend 500-detail leak. These were corrected and the final suites rerun. Additional
review fixed legacy allocator bypass, Group cooldown checks, crash-state history
reconciliation and the older evidence/session lifecycle blockers. The counts above
are distinct cases, not sums of repeated runs.

## Delivery and remaining gates

- Runtime changes span 40 application/migration/release-script files; eight test
  files are added/changed, plus three documents. The existing 29-file V20 patch is
  inherited from PR #1, not reapplied over production source.
- `fpm.service` was inspected read-only: active/running, MainPID 1369909, started
  2026-10-27 15:25:40 UTC, cwd `/root/facebook-pipeline-manager`. The last recorded
  release remains `group-publishing-20261027T152102Z`. No service operation occurred.
- Production DB, CTV assignments, jobs, cookies and browser profiles were not
  changed. No V20/V21 migration or production backup/deploy was run in this task.
- No real Facebook browser, live AI request or Publish was executed. There is no
  live permalink or new live preflight evidence. Local fixture links are not real
  publication evidence.
- Production release still requires source review/PR dependency resolution,
  historical license/provenance review, current read-only compatibility checks,
  verified WAL backup/restore rehearsal, separately approved V20/V21 maintenance,
  operator-authenticated production Dashboard and controlled live preflight.
- A joined Group, approved eligible image, verified identity/session and separate
  publication approval are mandatory before a live test. Do not override any gate
  to turn these isolated results into a claimed Facebook success.

See `multi-account-cycles-design.md` for the source audit/call graph and
`multi-account-cycles-release-runbook.md` for compatibility and rollback.
