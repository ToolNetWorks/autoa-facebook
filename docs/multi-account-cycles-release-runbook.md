# Multi-account cycles release preparation (V21)

Preparation only: this task does not authorize deployment, service control,
production migrations, CTV changes or Facebook publication. V20 PR #1 remains a
separate review dependency; do not merge it automatically.

## Before approval

1. Review the candidate commit and manifest, V20 PR #1, `NOTICE.md`, source audit
   and test results. Build UI from the same commit outside production static.
2. Inspect current service PID/cwd/command and schema read-only. Last known release
   is `group-publishing-20261027T152102Z`, schema V19. Previously authorized ownership
   maintenance selected CTV867 for Pipeline 1; verify the current single active
   assignment instead of relying on the old PR description's conflict warning.
   This release must never select or suspend a CTV itself.
3. Run existing `scripts/check_group_release.py --database PATH` read-only.
   Inspect `migrations.v020_unique_active_membership.plan(connection)` and
   `migrations.v021_posting_cycles.plan(connection)` with sqlite3.Row. A read-only
   connection uses `file:...?...mode=ro`; do not import `app` for this check.
4. Stop if V19 nickname columns are absent, V20 constraints missing/incompatible,
   membership conflicts remain, duplicate/missing Facebook fingerprints exist,
   canonical Group keys collide, or a required existing index has the wrong shape.
   A missing fingerprint requires an independently authorized identity backfill;
   V21 does not guess UID ownership or edit historical data.
5. Audit in-flight/queued legacy jobs before enabling cycle configuration. It
   suppresses future legacy allocation but does not delete old jobs. Open Group
   work blocks new assignments. Ambiguous submissions are never replayed.

## Approved maintenance sequence

1. Obtain a specific deployment/migration approval and maintenance window. Pause
   operator scheduling, drain active submissions to recorded outcomes, stop the
   single service gracefully under that approval and verify no publishing child
   or profile-lock holder remains. Never start a parallel worker.
2. Use SQLite's backup API to capture WAL content into a private 0700 backup
   directory with 0600 files outside Git/static. Preserve source/static/config,
   profiles and sessions without printing secrets. Do not copy only the main DB.
3. Restore into a separate path and verify integrity, foreign keys, row counts and
   historical IDs. Rehearse V20 then V21 on that copy in explicit transactions.
   Check `foreign_keys=ON`, before/after historical rows and schema ledger; confirm
   duplicate/cross-Pipeline writes are rejected. Do not run tests on production.
4. V20 adds unique active-membership constraints; V21 then adds cycle/Album/template/
   global-state tables and scoped/identity uniqueness indexes. Neither migration
   UPDATEs/DELETEs historical data, changes CTV ownership or repairs duplicates.
   Apply only after compatibility and verified backup, inside explicit transactions.
   Application startup never applies either migration.
5. Deploy backend, worker, selector config and separately built UI as one immutable
   release. Preserve .env, private data, cookies and profiles. The original checkout
   is dirty; do not reset it or copy an incomplete subset over running production.
6. Start exactly one approved service, check health, PID/source/UI checksums, logs,
   row counts, foreign keys and migration versions. Keep Auto Posting OFF until an
   operator reviews accounts, Groups, media, identity and effective Admin limits.
7. Operator uses existing credentials securely. Verify Admin and CTV Dashboard,
   two-Pipeline RBAC, session timestamps, Group URLs, actual History and report
   counts. A missing V21 schema keeps legacy UI/API functional; new cycle endpoints
   return CYCLE_SCHEMA_REQUIRED. Other server errors must remain visible errors.
8. Verify the persistent next due date, safe claims, profile locks and crash-review
   handling in staging. Preflight only with specifically authorized live context.
   Production does not yet have a verified joined Group/approved image from this
   task; do not create a real job or Submit merely to finish the release check.

## Rollback

- Before migration COMMIT, ROLLBACK restores the prior schema/data transaction.
- Before any new writes, a verified backup can be restored only under explicit
  rollback approval after checking that no later data would be lost.
- After new writes, never restore the whole old DB over them. Preserve all new
  outcomes, stop claims safely, retain additive V20/V21 tables/indexes and restore
  the previous complete source/static/config artifact. No DROP or history deletion.
- An older source does not understand pending cycle jobs. Before rolling back with
  such jobs outstanding, block all publishing in the maintenance window and obtain
  a reviewed, separately approved queue reconciliation. Do not run the old worker
  on new queued work or automatically resume old allocation for the same Groups.
- Do not undo prior CTV revocation, reactivate expired sessions, clear global Group
  disabled state or retry unresolved submissions as part of rollback.

## Release artifacts

Build with `npm run typecheck` and `npm run build -- --outDir PRIVATE_OUTPUT` in
`frontend/`. Generate the reviewed source/UI checksum manifest after commit using
`scripts/build_group_release_manifest.py --frontend-dir PRIVATE_OUTPUT --output
PRIVATE_MANIFEST`. The manifest states runtime legacy minimum V19 and cycle feature
requirements V20/V21; runtime compatibility does not mean cycles work without V21.
Artifacts stay outside Git. Deployment and Facebook Submit flags remain false.

No production maintenance command has been executed by this runbook preparation.
