# V20 release gate and rollback

Preparation only. No command in this document is authorization to deploy, migrate,
change CTV assignments, start a browser on a production profile, or submit a post.
A GitHub PR is for review; it is not a production deployment.

## Gate before approval

1. Review the exact integration commit, NOTICE/provenance and validation results.
   The repository's checked-in legacy static assets are not the new UI artifact:
   build frontend from the candidate and include its checksums in the manifest.
2. Run `python scripts/check_group_release.py --database PATH` read-only. The current
   V19 schema supports the runtime, but active membership conflicts block this
   release. V20 is never invoked by application startup.
3. The owner must choose the retained CTV. Preserve both users and all resources;
   suspension/revocation of the other assignment, if selected, requires a separate
   authorized transaction with before/after audit. Do not infer ownership from a
   lower user ID, uploader, or NULL owner fields. Do not move Facebook profiles.
4. Review historical attribution where a source Post ID was used as a Job ID.
   The source fix protects future writes and refuses conflicting rows; it does not
   repair old production data. A targeted repair must identify execution/tenant/
   account from evidence and receive separate approval. Never replay those jobs.
5. Confirm no in-flight browser/queued dispatch, profile lock holder or user login
   flow, and no unresolved Submit being retried. Read current state, not old counts.
   Obtain operator-provided authenticated Dashboard access; never replay DB cookies.

## Approved maintenance sequence

1. Freeze operator scheduling/approval for the maintenance window. Drain any active
   job to a recorded outcome. Stop fpm.service gracefully under deployment approval;
   verify the application and its publishing subprocesses have exited. Do not kill
   a Submit whose result is unknown or run a second worker alongside it.
2. Backup production with SQLite's backup API from a normal read connection so WAL
   content is included. Do not copy only the main SQLite file. Keep backups 0600 in
   a private 0700 directory outside static/Git; include source/static/config and
   profiles/sessions without printing their contents.
3. Open an independent restored copy. Check integrity/foreign keys and compare row
   counts/IDs plus preserved historical data. Rehearse migrations and approved data
   reconciliation on this copy first. Never run the test suite against production.
4. Apply only the separately approved CTV change. Re-run
   `migrations.v020_unique_active_membership.plan` with row_factory=sqlite3.Row.
   If any conflict or incompatible existing index remains, stop. Apply V20 inside
   an explicit transaction only after a verified backup. It adds two partial unique
   indexes and a version ledger, and does not UPDATE/DELETE history or choose a CTV.
5. Deploy backend, worker, services, API, selector config and built frontend from the
   same immutable candidate/manifest while service is stopped. Do not copy only the
   runner into a directory where the old backend may launch it. Preserve production
   data/env/profiles and the previous release. V19 nickname evidence stays intact.
6. Start one fpm.service instance under the approved window. Verify PID/cwd/command,
   health, source and UI hashes, migration ledger/constraints, row counts and logs.
   The candidate preserves expired/revoked web-session history; expiry still blocks
   access. Check queue recovery: uncertain results must not be requeued.
7. Operator logs in securely. Verify Admin Dashboard and each independent CTV scope,
   Post links, analytics error behavior, session timestamps and job history against
   DB. Use the operator's valid sessions; do not reset passwords to enable testing.
8. With separate preflight authorization, use the allowed account/Group/image/caption
   and the correct profile. `fb_browser.py preflight REQUEST RESULT` forces dry-run
   through the same runner and cannot be changed to Submit by request data. The
   request must come from a verified owned execution context, with positive IDs and
   an owned image; do not fabricate a production job. `dry-run` remains validation
   only, and must not be reported as browser verification.
9. Collect preflight screenshot, selector version/profile, target, identity and time.
   Stop for separate approval before creating/submitting one real test publication.
   No absence of errors, health response or mock permalink authorizes Publish.

## Rollback

- Before migration COMMIT: rollback its transaction; both indexes/ledger are atomic.
- If source/startup/UI fails: stop claims safely, preserve any new outcome and restore
  the previous complete source/static/config snapshot, then restart under rollback
  approval. Do not `git reset` the production dirty tree.
- Keep additive V19/V20 schema when reverting source. Do not drop indexes, restore
  expired sessions as active, re-enable a suspended CTV, change Group URLs or delete
  jobs. An older source may not handle membership conflicts safely; verify access.
- Never restore a whole DB over new writes. After COMMIT, use separately approved
  compensating repair unless the owner confirms the backup can be restored without
  losing any subsequent data. Test restore only in a separate database first.
- Selector `v2` profiles can be switched only through a reviewed release. Each running
  workflow retains its pinned snapshot. The archived `v1` profile is paired with the
  previous source release; do not activate v1 under new helpers requiring v2 keys.
  Roll back source and selector config together, not just active.json.

## Known release blockers

- Conflicting active CTV assignments still require an owner decision and approved
  V20 maintenance. Deploying the new auth guard first would deny both sessions.
- Historical attribution requires a reviewed repair, not an automatic migration.
- Production Dashboard after operator login and new live Facebook preflight are not
  verified by local tests. Nickname and post-submit verification require live evidence.
- Historical upstream provenance requires owner review before redistribution; see
  NOTICE. No new upstream code is vendored in this change.

No Facebook publication or permalink is claimed by this release plan.
