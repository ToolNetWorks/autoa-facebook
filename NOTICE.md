# Source references and provenance

ToolNet uses its own Python/Playwright runner, API, scheduler and database. It does
not install, import or vendor any of the four Facebook posting applications below.
Their behavior was compared with the runtime call graph; similarity of a technique
is not evidence of a licensed code transfer or of successful Facebook publication.

| Reference | Revision reviewed | Source license observed | Treatment |
| --- | --- | --- | --- |
| ariknih/autopost-facebook-group | f2ed0c8a2073aca87690dae68f153df8a03f6a36 | MIT, Copyright 2026 Arik Slebew | Navigation, scoped composition and upload techniques adapted in ToolNet |
| tekdt/AutoPostFacebookGroupSimple | d1ab58cb21b9191026cecb4282e28d53890b96da | MIT, Copyright 2025 TekDT | Caption readback and bounded control waits adapted; no Selenium code vendored |
| thenotoriousmrkrabs/opencli-plugin-fb-autopost | f613afdd65a84a99f6fe69590bd8df77f4c03fa9 | Apache-2.0; NOTICE.md identifies Copyright 2026 Ang Jing Kang | Preflight, preview and outcome techniques adapted; no Browser Bridge code vendored |
| WahuVN/facebook-group-poster | 7ab29997de3e3162ac8a55cb1165f732462ac7e7 | No root LICENSE; NOT_PUBLIC_RELEASE.txt restricts distribution pending owner review | Reference only for this release; no upstream source copied in this change |

The previous session/retry/logging implementation had blocks similar to the Wahu
source. This change replaces those implementations against ToolNet's behavioral
contracts: private durable session replacement, bounded pre-submit retry and
append-only redacted event logging. It does not assert a clean-room development
process, resolve the provenance of historical commits, or grant rights in any
upstream work. Historical provenance still requires owner review before external
redistribution. No repository history or attribution has been removed.

If a future change actually copies licensed source, retain the corresponding
copyright/license and applicable notices with that code. This notice does not
select a license for ToolNet, relicense upstream work, or imply endorsement by
Meta, Facebook or the referenced authors.
