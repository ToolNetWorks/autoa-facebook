# Integration candidate validation — 2026-10-27

## Executed checks

| Check | Result | Scope |
| --- | --- | --- |
| Comprehensive regression | 453 passed, 0 failed/errors/skipped; 349.58s | 31 selected Python modules, isolated SQLite and local browser |
| Final affected-module regression | 43 passed, 0 failed/errors/skipped; 74.76s | Hardening, API→worker→Dashboard E2E, Dashboard UI, worker contract, dispatch, analytics |
| Distinct test cases across both runs | 454 passed | The final run adds one diagnostics-sanitization test; repeated cases are counted once |
| Frontend typecheck | PASS | `npm run typecheck` |
| Frontend production build | PASS | `npm run build -- --outDir /tmp/toolnet-integration-v20-frontend` |
| Responsive browser assertions | PASS | 390, 402, 430, 440, 768, 834, 1280, 1440px; account state, Dashboard errors, Auto Job links/history |
| Diff whitespace | PASS | `git diff --check origin/main` and staged checks |

The final UI screenshot was inspected: the confirmed-post KPI and history both
show the single verified local-fixture result. The test asserts that count, Post
link, attribution, one Submit click, no second claim and rejection of requeue.

Earlier regression attempts exposed an obsolete scheduling assertion and a fixture
that did not record authoritative membership; both were corrected and rerun. The
image-path fixture and UI KPI mismatch were also fixed. These final results do not
claim the entire repository's unrelated video/cloud test suite was executed.

## Reproduction

Use a private disposable DATA_DIR, synthetic credentials from test fixtures and
the `offline_runtime` plugin. Do not run these commands in the production checkout
or point DATA_DIR at live data. Browser tests require local Chromium support.

```sh
DATA_DIR=/tmp/toolnet-integration-release PYTHONPATH=.:tests/helpers \
  /root/facebook-pipeline-manager/.venv/bin/python -m pytest -p offline_runtime \
  tests/test_membership_invariant.py tests/test_campaign_persistence.py \
  tests/test_facebook_publishing_dom.py tests/test_dispatch_e2e.py \
  tests/test_pipeline_dispatch_e2e.py tests/test_integration_hardening.py \
  tests/test_group_worker_contract.py tests/test_dashboard_blockers_ui.py \
  tests/test_account_session_ui.py tests/test_publishing_blockers.py \
  tests/test_post_verifier.py tests/test_group_management.py tests/test_accounts_v5.py \
  tests/test_first_post_flow.py tests/test_group_verification.py \
  tests/test_smart_schedule_v9.py tests/test_multitenant.py tests/test_ownership_v6.py \
  tests/test_retry_policy.py tests/test_logging.py tests/test_pipeline_auto_post_v8.py \
  tests/test_group_campaign.py tests/test_analytics_v7.py tests/test_ctv_album.py \
  tests/test_media_album.py tests/test_media_cloud.py tests/test_group_eligibility.py \
  tests/test_group_membership.py tests/test_daily_limits_timezone.py \
  tests/test_group_screening.py tests/test_group_classification.py \
  -q --disable-warnings --maxfail=3 --tb=short -o faulthandler_timeout=45 \
  --junitxml=/tmp/toolnet-integration-release.xml
```

The command above now collects 454 cases after the additional final test. The
recorded comprehensive run collected 453; the 43-case final run used the first six
modules below after the last KPI/history/UI edits:

```sh
DATA_DIR=/tmp/toolnet-integration-final-ui PYTHONPATH=.:tests/helpers \
  /root/facebook-pipeline-manager/.venv/bin/python -m pytest -p offline_runtime \
  tests/test_integration_hardening.py tests/test_pipeline_dispatch_e2e.py \
  tests/test_dashboard_blockers_ui.py tests/test_group_worker_contract.py \
  tests/test_dispatch_e2e.py tests/test_analytics_v7.py \
  -q --disable-warnings --maxfail=2 --tb=short \
  --junitxml=/tmp/toolnet-integration-final-ui.xml
```

Private logs: `/tmp/toolnet-integration-release.log` and
`/tmp/toolnet-integration-final-ui.log`, with corresponding JUnit XML. Screenshots
remain outside Git in `/tmp/toolnet-remaining-ui` and `/tmp/toolnet-blockers-ui`.
They depict synthetic local data, not Facebook publication evidence.

## Release boundaries

- The 29-file staging patch is incorporated into normal branch source, together
  with runtime wiring and the additional fixes. It is not applied to production.
- The original checkout's 219 baseline files remain byte-identical. Production
  schema was inspected read-only; V19 nickname fields exist. V20 is not applied.
- Active membership conflict blocks deployment; neither CTV was changed. V20 must
  be separately approved after the retained assignment is chosen and reconciled.
- Local authenticated Dashboard is verified. Production Dashboard after operator
  login, new live preflight and post-submit verification remain unverified.
- No real job, real Facebook Submit, live permalink, production restart or deploy
  was performed. Mock success is not live success.
- The final manifest is generated after commit with
  `scripts/build_group_release_manifest.py`; it binds source and separately built
  UI hashes. Checked-in legacy static assets must not replace that UI build.
- Review source provenance in NOTICE and the release/rollback runbook before any
  release approval. No migration, deployment or publication is authorized here.
