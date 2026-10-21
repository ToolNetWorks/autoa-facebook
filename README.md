# ToolNet Facebook Pipeline Manager V5 — Multi-Tenant + Per-Pipeline RBAC

## Accounts and roles

- **Chủ hệ thống (Admin)** signs in with username and password. The admin sees the global dashboard, creates and locks pipelines, issues Pipeline Access Keys, manages collaborators and their permissions, and reads the activity log.
- **Cộng tác viên (CTV)** signs in with **username + password + Pipeline Access Key**. All three are verified: the account must be active, it must be assigned to a pipeline, the key must belong to that same pipeline and must not be revoked or expired, and the collaborator must hold the required permission. The key is a second factor, never a standalone grant — a collaborator who learns another pipeline's key still cannot enter that workspace.
- A collaborator is pinned to a single workspace. The backend never trusts a `pipeline_id` sent by the browser: the effective pipeline comes from the membership row, and every entity lookup (account, group, post, job, manual task, media file, screenshot) is checked against it. A row of another workspace is reported as **404 Not found** so nothing leaks; a row inside your own workspace that needs a permission you lack is reported as **403 Forbidden**.
- The principal is rebuilt from the database on **every** request, so suspending a collaborator, changing their permissions, moving them to another workspace or revoking a key takes effect on the very next request.
- Passwords and access keys are stored only as Argon2id hashes. Session cookies are HttpOnly, SameSite=Strict and carry a CSRF token that every mutating request must echo in `X-CSRF-Token`. Login attempts are rate limited per IP and username.

### Permissions

`facebook.view`, `facebook.add`, `facebook.update_session`, `groups.view`, `groups.manage`, `reels.create`, `reels.publish`, `group_posts.create`, `group_posts.publish`, `comments.manage`, `ai_caption.use`, `schedule.manage`, `history.view`.

A new collaborator starts with read access and draft creation only. Publishing permissions must be granted explicitly. Without `group_posts.publish` a scheduled group post is stored as a draft (`pending_approval`) and waits for the administrator; without `reels.publish` a Reel task stays manual. Collaborators never receive cookie values, browser storage state, password hashes or key material.

### Pipeline Access Keys

Each workspace owns a key generated with the OS CSPRNG (`FPM-` + 160 bits). The full value is shown exactly once, when it is created; only its prefix and metadata remain in the database. Issuing a new key revokes the previous one and ends the sessions bound to it.

## Workspace behavior

- Dashboard has an independent card for each pipeline. Example `@hiu9h` → its own workspace, accounts, groups, queue and tasks.
- **Personal Profile Reels:** upload MP4/MOV up to 200 MB, caption and schedule; select the Facebook account. Optional checkbox enables experimental automated publishing via the account's logged-in Playwright browser. There is **no official API for personal-profile Reel posting** and live browser publication has NOT been validated. If the desktop Reel composer cannot be confirmed, worker stops; it does not silently publish a regular video post. A click to Publish results in `submitted_unverified` until you confirm on Facebook.
- **Facebook Group anonymous image posts:** each new group post requires 1–4 images + caption, chosen groups and schedule. The browser worker must verify Facebook's anonymous toggle/author mode. If the group or account does not support anonymous publishing, it **fails closed**; it must never fall back to a named post. Anonymous does not conceal the author from group admins/moderators or Meta. Submission can be pending moderator approval.
- **Comments:** schedule and manage manually; the system DOES NOT automatically submit mass comments.
- Login via browser or own exported JSON cookie file, update cookie for existing account. No CAPTCHA bypass or automatic reattempt of uncertain submissions.
- V4 migrates schema without deleting data. **Legacy queued group posts are marked `needs_review` instead of being submitted under a real name.** Existing submitted/published history remains unchanged. Old Reel/manual task records remain intact.
- V5 adds the tables `users`, `pipeline_memberships`, `pipeline_access_keys`, `user_sessions`, `audit_logs` and `ai_caption_settings`, and adds an ownership column to `jobs`. Every step is additive; existing pipelines, accounts, groups, posts, jobs and manual tasks are preserved and backfilled.
- **AI Caption:** the `ai_caption.use` permission unlocks caption suggestions that are produced by a local template engine inside this server. No topic, caption or API key ever leaves the machine, and no external AI provider is contacted.

## Pipeline media album and automatic group posting

Every workspace owns an album of pictures and a configuration for posting them automatically.

### The album

- Upload one picture or many at a time. The file is validated by its real signature, not by the name, the size is limited and the key of the file is random.
- The pictures are shown as a grid, open in a large view, carry their name, size, resolution, tags, description, safety state and how often they were used.
- Tags, a search field and a filter for the usable pictures narrow the list down.
- A picture can be marked as usable, locked, kept for a later schedule, analysed again or deleted. A picture that a pending job still uses is not deleted.
- The files live in a private folder of the server. They are never written into a public directory and they are only served after the membership of the workspace and the permission of the caller were checked.

### What is stored for every picture

The media id, the pipeline, the collaborator who uploaded it, the original name, the storage key, the mime type, the width and the height, the size, the sha256 and the picture signature, the tags, the description of the AI, the safety state, how often it was used, when it was used last and when it was uploaded.

### The analysis of a picture

The picture is validated, a small preview is generated, the properties are read, the content is checked for risk, the vision model describes it and writes internal tags, and the album is searched for a duplicate. The description is written for the machine and never invents a product, a price or an advertising promise. A picture with a risk stays `NEEDS_REVIEW`, and a picture that is `BLOCKED` is never used for an automatic post.

### Automatic group posting

*Auto Group Posting* holds the configuration: on or off, the Facebook account, the groups, the topic or the sample caption, whether the AI writes the caption, the daily limit of the account and whether a post has to be approved before it is published.

The automatic run follows the same steps every time:

1. the groups that qualify are taken from the group screening engine, so a group that was never checked or that is restricted is left alone,
2. the anonymous or nickname mode of every group is verified,
3. one picture is chosen from the album,
4. the AI writes or optimises the caption from the picture, the topic and the guidance,
5. the caption safety and the media safety run,
6. a preview shows the picture, the caption, the group, the account and the mode,
7. the job is queued when the checks passed and the approval mode allows it,
8. the worker publishes it and records what really happened.

**One post is always one picture, one caption and the mode of the group.** When it is not clear whether a post was sent, it becomes `SUBMITTED_UNVERIFIED` and is never sent again on its own.

### How a picture is chosen

The choice is never random and the reasons are shown. A picture scores higher when it matches the topic, when it passed the safety check, when it was not used for a while, when it fits the rules of the group and when it was not used too often. A picture that is blocked, locked, not marked as usable or that still carries an open warning is never chosen. When the album has nothing suitable the job stops and asks for more pictures.

Changing the picture or rewriting the caption is never a way around the anti spam rules of Facebook, and the history of every picture is kept per account and group.

### API

- `POST /api/pipelines/{id}/media/upload` — upload one or many pictures.
- `GET /api/pipelines/{id}/media` — the album with its counters, tags and filters.
- `GET /api/pipelines/{id}/media/{media_id}` — one picture with its analysis and its usage history.
- `GET /api/pipelines/{id}/media/{media_id}/file` and `.../thumbnail` — the picture and its preview.
- `PATCH /api/pipelines/{id}/media/{media_id}` — tags, description, usable, locked, saved.
- `DELETE /api/pipelines/{id}/media/{media_id}` — remove a picture.
- `POST /api/pipelines/{id}/media/analyze` — queue the analysis of the pictures that wait for it.
- `POST /api/pipelines/{id}/auto-post/config` — store the configuration.
- `POST /api/pipelines/{id}/auto-post/preview` — what would be prepared right now.
- `POST /api/pipelines/{id}/auto-post/schedule` — prepare the posts of the day.
- `POST /api/pipelines/{id}/auto-post/{job_id}/approve` — accept a prepared post.
- `GET /api/pipelines/{id}/auto-post/history` — the prepared and published posts.

### The permissions of a collaborator

`media.view`, `media.upload`, `media.edit`, `media.delete`, `media.approve`, `auto_post.configure`, `auto_post.create`, `auto_post.approve` and `auto_post.publish` are granted by the administrator one by one. A collaborator may not switch off a mandatory safety check, may not approve their own pictures unless the administrator gave the permission, and may not reach the album, the pictures or the reports of another workspace.

## AI video pre-check before publishing a Reel

Every Reel is checked before it is scheduled. The panel in **Reels · Trang cá nhân** shows the video preview, the file, the progress of the check, the report and the buttons *Kiểm tra lại*, *Dùng caption AI* and *Chủ hệ thống duyệt*. The button that schedules the Reel only opens when the check passed or an administrator accepted it.

### The five categories

| Category | What is inspected |
| --- | --- |
| technical | the file is read with ffprobe: codec, container, duration, resolution, aspect, frame rate, bitrate, plus black stretches, frozen frames and the audio level. A file that cannot be read or does not meet the requirements is blocked. |
| visual | key frames are cut out of the video and read by the vision model for violence, blood, sexual content, harassment, dangerous acts, misleading advertising, risky text and imagery, watermarks and poor quality. |
| audio | the audio is extracted with ffmpeg and transcribed. Abusive or agitated wording, misleading advertising, unsuitable speech and distorted audio are reported. A failure of the transcription is not a violation. |
| caption | the caption goes through the caption validator and the caption model: spam, repeated or unrelated hashtags, abusive wording, suspicious links, misleading content and the match between the video and the caption. |
| copyright | the file hash and a picture signature are compared with the media of the same workspace, so an exact or a near duplicate is found. Finding no duplicate is not a proof of the copyright. |

The result is `PASS`, `NEEDS_REVIEW` or `BLOCKED`. A blocked Reel is never queued. A category that could not be checked stays *unknown*, and one unknown category keeps the whole result at `NEEDS_REVIEW`: a failure or a timeout of the model never turns into a pass. The administrator can accept a result that needs a person, and the acceptance is recorded separately from the evidence.

The report lists the internal risk level with an explanation, the categories that were checked, the categories that could not be checked, the reason of every warning, the moment in the video, the related frame, the related words, the suggested fix and the final state. The score of the model is never turned into a promise that Facebook will accept or reject the Reel.

### The provider

`AI_BASE_URL` (`AI_URL`), `AI_API_KEY` (`AI_KEY`) and `AI_MODEL` are already used for the captions. Two more are optional:

| Variable | Meaning |
| --- | --- |
| `AI_VISION_MODEL` | model that reads the key frames. Without it the visual category stays unknown. |
| `AI_ASR_MODEL` | model that transcribes the audio. Without it the audio category stays unknown. |

No media leaves the server unless a provider is configured and the model is set. The key is never written to a log. The technical check, the fingerprint and the duplicate search all run locally.

### The publish guard

Before a Reel is scheduled the backend checks that the pre-check belongs to the same workspace and to the same account, that the file and the caption are still the ones that were checked, that no blocked or unresolved result is left, and that the other limits of the account are respected. A Reel whose file or caption was changed after the check has to be checked again. The guard runs in the backend and in the worker, not only in the browser.

### API

- `POST /api/pipelines/{id}/reels/precheck` — queue the check of a video.
- `GET /api/pipelines/{id}/reels/precheck/{check_id}` — the state and the report.
- `POST /api/pipelines/{id}/reels/precheck/{check_id}/retry` — run it again.
- `POST /api/pipelines/{id}/reels/precheck/{check_id}/approve` — an administrator accepts the result.
- `GET /api/pipelines/{id}/reels/precheck/history` — the checks of this workspace.

The checks run in a background queue, one at a time, so a long video never blocks a request. The temporary frames and audio are deleted afterwards, the original file is kept.

## Automatic group screening and the priority engine

Every group is classified from evidence that can actually be observed, and the classification decides the order in which the eligible jobs are served.

### The five states

| State | Meaning |
| --- | --- |
| `priority` | recent evidence that the posts of this account go out directly, without approval. Served first. |
| `review` | a notice in the browser or in the history shows that a post waits for an administrator. Served after the priority groups and tracked as `pending_approval`. |
| `unknown` | not enough evidence to say whether an approval is needed. Never treated as priority. The group waits until it is checked. |
| `restricted` | no right to post, not a member, blocked, or the account lost the right. The related jobs are held. |
| `failed` | the group could not be reached or the check broke. The reason is stored and the check is retried with a sensible rate. |

The state belongs to the pair of account and group, because the same group can behave differently for different accounts. An administrator may confirm a state by hand, but such a result is marked `manual` and never mixed with evidence. A group can also be switched to manual checking, which forces it back to unknown.

### What the screening looks at

* the account was signed in, the group was reachable, the account is a member, the composer is available,
* whether the composer offers the anonymous or the nickname identity, and whether the configured nickname exists,
* whether a notice about an approval by an administrator is visible, and what the group rules say when they are readable,
* the posting history of that account in that group: how many posts went out directly, how many are still waiting, how many were rejected or removed.

**No post is ever published to find this out.** A single successful post is evidence but not a guarantee, so the confidence of a priority group stays clearly below certainty. When the Facebook interface changes, the result is unknown rather than a guess.

### The priority engine

Only the order of the jobs that were allowed to run anyway changes. The priority never skips the daily limit of an account, the pause between two posts, the rules of the group, the result of the AI safety engine, the permissions of a collaborator, or the anonymous and nickname mode that was chosen. A group in unknown, restricted or failed state is not served automatically.

### API

- `POST /api/pipelines/{id}/groups/screen` — queue a check for the given groups.
- `POST /api/pipelines/{id}/groups/screen-bulk` — queue a check for every group of the workspace.
- `GET /api/pipelines/{id}/groups/screening-results` — every result with its evidence and the counters.
- `GET /api/pipelines/{id}/groups/priority` — the groups in the order they should be served.
- `POST /api/pipelines/{id}/groups/{group_id}/recheck` — queue another check for one group.

The checks run through a queue with a rate limit and only one browser session at a time, so screening never floods Facebook. A group is not checked again inside the cooldown unless the check is forced.

## Group management and posting identity

Every pipeline owns its own groups. A group is bound to a Facebook account of that pipeline and to one posting mode.

- **Add a group**: paste the group link. A full URL, a group id, a group slug and the `/share/g/<code>` form of a shared group are all accepted. The link is normalised to `https://www.facebook.com/groups/<id>/` and every tracking parameter is removed. A group that is already in the workspace is refused, including the case where it arrives under its other identifier, because Facebook addresses the same group both as `/groups/<id>` and as `/share/g/<code>`.
- **Add many groups**: one link per line. The answer reports how many links were valid, how many were duplicates and how many were invalid, with the reason for each invalid one. Nothing is posted automatically.
- **Posting modes**: `Anonymous` posts under the anonymous identity, `Nickname` posts under a nickname the operator configured. A nickname in the tool is only a wish; it does not mean Facebook created or accepts it. The system never creates a nickname through an unofficial API and never skips Facebook's own confirmation step.
- **Check a group** uses the account's own Playwright session and reports what could be confirmed: signed in, group reachable, joined, composer available, anonymous supported, nickname supported, the configured nickname present, and whether the group asks an administrator to approve posts. Anything that cannot be confirmed is reported as unknown or needs review, never as "allowed to post". The last check time is stored with the group.

### The rules the worker follows

- A group set to Anonymous is only ever posted anonymously. A group set to Nickname is only ever posted with exactly the configured nickname.
- When the requested identity cannot be verified, the worker does not click publish. The job ends as `needs_review` with the reason, and the profile name is never used as a substitute.
- The identity is verified again right before the submission, so a mode that was lost in the meantime also stops the post.
- A group that asks for approval is reported as `pending_approval`, which is not the same as `posted`.
- A job is never retried blindly: the outcome of a possible publication is stored, and a new attempt has to be created by hand after checking Facebook.

### API

- `GET|POST /api/pipelines/{id}/groups` — list or add a group.
- `POST /api/pipelines/{id}/groups/bulk` — add many groups from a list of links.
- `PATCH|DELETE /api/pipelines/{id}/groups/{group_id}` — change the account, the mode, the nickname or the state, or remove the group.
- `POST /api/pipelines/{id}/groups/{group_id}/check` — check the group with the account's own session.
- `POST /api/pipelines/{id}/groups/preview-check` — check a link before it is saved.
- `POST /api/pipelines/{id}/group-posts` — schedule one post for several groups, one job per group with its own identity.

All of them verify the caller, the permission and the pipeline membership. A group, an account or a job of another workspace is reported as not found. Reading the groups needs `groups.view`, changing them needs `groups.manage`, and scheduling a post needs `group_posts.create`; publishing automatically additionally needs `group_posts.publish`.

**Facebook Groups API is retired.** Everything here goes through Playwright and is experimental, so a change of the Facebook interface can break it. Do not claim that anonymous or nickname posting works without testing it on Facebook itself.

## AI Caption Automation

Each workspace can optimise its captions before they are scheduled. The panel sits in the posting pages (group photos and personal Reels) and works on the caption the operator typed.

- **Caption gốc** keeps the original text and is never overwritten. **Caption AI** shows the rewritten caption and can be edited by hand.
- Buttons: **AI tối ưu caption**, **Tạo lại**, **Dùng caption AI**, **Khôi phục bản gốc**. The panel shows the changes between the two texts and the verdict: *Phù hợp* / *Cần xem lại* / *Không phù hợp*.
- Options per workspace: bật tự động tối ưu caption, giữ nguyên ý nghĩa, tự động thêm hashtag, tối ưu cho Reels, tối ưu cho Group, tự động dùng kết quả AI khi đạt kiểm tra an toàn. Only the administrator may switch the automatic mode on or off.

### Vietnamese specifics

The local checks understand how operators actually write in Facebook groups:

- **Compact numbers.** `2k3` means 23, `5k` means 5000, `1tr2` means 1.2 million. Facts are compared by value, so a rewrite that turns `2k3` into `23` keeps the meaning while a rewrite that turns it into `30` is caught.
- **Abbreviated captions.** `dc`, `lun đê`, `Lh TEIE` and similar forms are treated as ordinary text. The number of words that survive the rewrite is checked, so a caption must not be reduced to a fragment.
- **Contact details.** A phone number, a link or a `@handle` that the operator did not mention is treated as invented and blocks the approval. So is a new contact channel such as Telegram, Zalo or Messenger, which would move the conversation off the platform without being asked.
- **Obfuscation.** Wording that looks like it hides a word from a filter is reported for a human, never quietly normalised.

### The four steps

1. **Analyze** — detects abusive, discriminatory, adult, violent, deceptive, spammy and over-promising wording in Vietnamese, and extracts the facts that must survive (numbers, measurements, links, phone numbers).
2. **Rewrite** — asks the configured provider for a natural rewrite, or runs the local meaning-preserving cleanup when no provider is configured. The local pass only tidies formatting, repetition and hashtags; it never adds, removes or rephrases a word.
3. **Validate** — the rewritten caption is analysed again, the key facts are checked and the verdict is computed by this backend. The model's own answer is never trusted.
4. **Publish decision** — `APPROVED`, `NEEDS_REVIEW` or `BLOCKED`. Only an approved caption may be queued; a blocked or unreviewed caption leaves the job in the queue for a person. A provider error, timeout or unusable answer keeps the job unsent and is reported.

Wording that breaks the rules in the **original** caption is reported and must be fixed in substance by the operator. The system never rewrites a violation into something that only looks acceptable, and it never uses character substitutions, hidden text, homoglyphs or other tricks to get past a filter.

### Provider configuration

Read from the environment, never hardcoded and never written to a log:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AI_BASE_URL` (`AI_URL`) | OpenAI-compatible base URL | — |
| `AI_API_KEY` (`AI_KEY`) | API key | — |
| `AI_MODEL` | default model | `gpt-4o-mini` |
| `AI_TIMEOUT` | seconds per request | `30` |
| `AI_MAX_RETRIES` | extra attempts on a transient failure | `2` |

A workspace may override the model in its caption settings. The answer is read as a JSON object and validated; a gateway that answers with a server-sent-event stream or appends the stream terminator is understood as well.

### API

- `POST /api/pipelines/{id}/caption/optimize` — analyse, rewrite and validate a caption.
- `POST /api/pipelines/{id}/caption/approve` — accept a caption for use after re-checking the edited text.
- `GET /api/pipelines/{id}/caption/history` — the caption records of that workspace.
- `GET|PATCH /api/pipelines/{id}/caption/settings` — the per-workspace configuration.

All of them require `ai_caption.use` and are scoped to the workspace of the caller. Records keep the original caption, the optimised caption, the status, the risk level, the model, the issues, the changes and the approval time.

**The assessment of the caption is not a guarantee.** Facebook decides on its own whether a post is accepted, and the account, the group, the media and the posting behaviour influence that decision.

## Safety and operational limits

- Only publish to groups where the account is permitted to post and only for your own accounts.
- Facebook Groups API was discontinued; anonymous groups and personal profile Reels are **experimental browser flows**, not supported official publishing APIs. DOM changes, upload constraints, login checkpoints, and moderator decisions can cause failures. No live Facebook posting test was performed.
- Never expose VNC publicly. Protect the dashboard with a strong password and preferably Cloudflare Access. Login cookies are sensitive.
- `APP_PASSWORD` **must be at least 12 characters**. If it was changed to `admin123`, the current backend will refuse to start and cause Cloudflare 502; set a stronger password before starting. Do not print passwords into logs.

## Update on existing root VPS

Place `facebook-pipeline-manager-v4.zip` at `/root/facebook-pipeline-manager-v4.zip`. Use Termius SSH as root:

```bash
set -e
cd /root
umask 077
STAMP=$(date +%Y%m%d-%H%M%S)
test -f /root/facebook-pipeline-manager-v4.zip
systemctl stop fpm
# Back up current application source and SQLite DB before migration.
tar --exclude='facebook-pipeline-manager/.venv' \
    --exclude='facebook-pipeline-manager/data' \
    -czf "/root/fpm-before-v4-$STAMP.tar.gz" facebook-pipeline-manager
if [ -f /root/facebook-pipeline-manager/data/manager.sqlite3 ]; then
  DB_BACKUP="/root/fpm-database-before-v4-$STAMP.sqlite3" \
  /root/facebook-pipeline-manager/.venv/bin/python - <<'PYCODE'
import os, sqlite3
source = sqlite3.connect('/root/facebook-pipeline-manager/data/manager.sqlite3')
target = sqlite3.connect(os.environ['DB_BACKUP'])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PYCODE
fi
unzip -o /root/facebook-pipeline-manager-v4.zip -d /root/
systemctl start fpm
sleep 5
curl -fsS http://127.0.0.1:9088/health
systemctl is-active fpm
```

No change to `/etc/systemd/system/fpm.service`, `fpm-desktop.service`, or `fpm-tunnel.service`. Refresh the existing Cloudflare URL after the health check. Ensure `APP_PASSWORD` is strong, and protect the dashboard behind access control when used over the public Internet.

**Public tunnel note:** Cloudflare may reject larger uploads even though the local server accepts a 200 MiB video; a private/VPN connection is preferable for large media. Keep noVNC and session cookies private.


## Usage

1. Sign in. The **Chủ hệ thống** uses username and password only. A **cộng tác viên** also enters the Pipeline Access Key that the administrator issued for their workspace; they land straight inside that workspace and never see the global dashboard.
2. **Chủ hệ thống → CTV & phân quyền**: create a collaborator (username, password, workspace, permissions). **Pipeline Key**: issue or revoke the access key of a workspace; the secret is shown once. **Nhật ký**: read the activity log.
3. The first administrator is created from `APP_USERNAME` / `APP_PASSWORD` in `.env` the first time the service starts. If that password is missing, shorter than 12 characters or a known-weak one such as `admin123456789`, the account is flagged `must_change_password` and every data endpoint refuses to run until the password is rotated from the dashboard. After that the password lives in the `users` table and `APP_PASSWORD` is no longer the source of truth.
4. Visit Dashboard, select the pipeline card e.g. `@hiu9h` to enter its separate Workspace.
2. **Tài khoản Facebook**: press **+ Thêm tài khoản Facebook** to open the modal, name the account and pick one of two connection methods.
   - **Upload Cookie JSON** — upload a `.json` file exported from your own session. A plain cookie array, `{"cookies": [...]}` and the Playwright `storageState` shape are all accepted. The file is validated (Facebook domains only, `c_user` + `xs` present, 250 cookies, 256 KB max) and is never stored; only the resulting cookies land in that account's own Chromium profile.
   - **Login Facebook** — opens a dedicated browser for that account so you can sign in and clear any 2FA or checkpoint yourself. Your Facebook password is never stored.
   Each card shows avatar, Facebook name and UID when they can be read, the connection method, the session state and the time of the last check, with **Kiểm tra**, **Cập nhật Cookie**, **Đăng nhập lại** and **Xóa tài khoản**. Uploading a new cookie file refreshes an expired account in place — no need to recreate it.
3. **Kiểm tra phiên** (`POST /api/accounts/{id}/check`) opens the account's own profile and reports `connected`, `expired`, `checkpoint`, `busy` or `error`. It never solves a CAPTCHA, a checkpoint or a 2FA challenge, and a structurally valid cookie file is never reported as a successful login. Cookie values are never returned by the API or written to the log.
4. **Nhóm Facebook**: add group link and assign the account authorized to post.
5. **Group · Ảnh ẩn danh**: select 1–4 images, enter caption, tick groups, choose schedule, confirm anonymous-only, submit. If anonymous toggle or mode is not verified on Facebook, the worker does not click Publish.
6. **Trang cá nhân · Reels**: choose Facebook account, MP4/MOV video, caption, schedule and opt in to experimental automatic browser posting. If Reel creation is unavailable, status is `failed`. If Publish was clicked but uncertain, status is `submitted_unverified` or `needs_review`; check the profile before any new attempt.
7. **Bình luận**: tasks remain manual only; no bulk automatic commenting.

**Account isolation.** Every account belongs to exactly one pipeline and owns a separate Chromium profile under `data/profiles/account-<id>` (mode `0700`). Accounts, cookies, sessions and groups of one pipeline are never reused by another. A cross-process advisory lock (`data/locks/account-<id>.lock`) prevents two Playwright sessions from driving the same profile at once, so a cookie import, a login browser and a posting worker cannot corrupt one another. The Facebook UID is stored encrypted at rest (`session_crypto.py`); the existing `fb_uid_fingerprint` keeps a cookie file from being attached to a different Facebook account.

The worker uses one serialized job lane. Default caps: five posts/reels per account per day and minimum ten minutes between attempts; minimum 24 hours between group posts for the same group. These limits do not guarantee compliance with Facebook's policies or account safety. Never automatically retry an uncertain submission.

## Diagnostics

```bash
systemctl status fpm fpm-desktop fpm-tunnel --no-pager
journalctl -u fpm -n 60 --no-pager
curl -fsS http://127.0.0.1:9088/health
```

If `/health` fails after changing the admin password, note the 12-character minimum. Check `journalctl -u fpm` before restarting again.

## Tests

```bash
cd /root/facebook-pipeline-manager
.venv/bin/python -m pytest -q tests
```

Unit tests cover auth, isolation, group rules, migration of old queued jobs, Reel account targeting, cookie input, and task states. **They do not verify that Facebook accepts live browser submissions**; perform manual end-to-end tests in a group and account you control. The current Playwright selectors may require updates to match Facebook's UI.

## Data preservation

Upgrade does not overwrite `.env`, `.venv`, or the `data/` directory. Back up SQLite using its backup API, not by copying the file while WAL writes are active. A migrated queued legacy group job will be set to `needs_review` so it cannot accidentally publish under the account name. Browser profiles and cookies are unchanged, and legacy manual tasks remain available.

## Album ảnh trên Cloudflare R2

Album của Workspace nằm trong bucket R2 `album-ctv`. Mỗi Workspace một thư mục, mỗi CTV một thư mục con:

```
album-ctv/
  @hiu9h/                        thư mục của Workspace
  @hiu9h/chung/                  ảnh của kho dùng chung (Admin tạo)
  @hiu9h/chung/thumbs/           ảnh thu nhỏ
  @hiu9h/hiu9h/                  ảnh riêng của CTV "hiu9h"
  @hiu9h/hiu9h/thumbs/
```

- Cấu hình nằm trong `.env` (`R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_API_TOKEN`, `R2_ACCOUNT_ID`, `R2_REGION`).
- Không có gì công khai: ảnh chỉ đọc được qua API của ứng dụng, mỗi request đều kiểm tra Workspace và chủ sở hữu.
- Ảnh tải lên được ghi thẳng lên R2, file cục bộ chỉ là chỗ tạm để sinh ảnh thu nhỏ rồi xóa đi. Ảnh cũ chỉ còn trên đĩa vẫn đọc được nhờ fallback cục bộ.
- Worker phân tích ảnh và worker đăng bài tự động đều đọc trực tiếp từ R2.
- Xóa ảnh sẽ xóa luôn đối tượng trong bucket.
- Chuyển ảnh cũ lên R2 lần đầu: `python scripts/migrate_album_to_r2.py` (chạy lại được, không ghi đè ảnh đã có trên cloud).
- Bật/tắt: bỏ `R2_*` khỏi `.env` rồi `systemctl restart fpm.service` để quay về lưu trên đĩa.

## Tự động đăng Group cho cả Pipeline (V8)

Chỉ cần **cấu hình một lần** ở cấp Pipeline; hệ thống tự chia công việc cho từng tài khoản
Facebook theo hạn mức riêng của tài khoản đó.

### Giao diện

Trang *Tự động đăng group* trong Workspace gồm:

* Sáu số chính: Facebook đang hoạt động, tài khoản đã đủ hạn mức, group đủ điều kiện,
  bài đã xác nhận đăng, bài đang chờ, bài bị chặn trùng.
* Một card cấu hình: bật/tắt, áp dụng cho tất cả hay một số Facebook, số bài tối đa
  mỗi Facebook mỗi ngày, group áp dụng, chủ đề mẫu, AI Caption, chống đăng trùng,
  cần duyệt trước khi đăng, áp dụng cho Facebook mới, và **Cài đặt nâng cao**
  (khoảng cách tối thiểu giữa hai bài, hạn mức tổng của Pipeline, múi giờ).
* Bảng hạn mức theo từng Facebook: số bài đã dùng, hạn mức riêng (để trống là dùng chung),
  phần còn lại, trạng thái và nút tạm dừng.
* Bảng **Group chưa đủ điều kiện** với lý do bằng chữ, ví dụ "Facebook hạn chế quyền đăng
  trong group này" hoặc "Chưa kiểm tra quyền đăng".

### Hạn mức

* Mỗi tài khoản có bộ đếm riêng theo múi giờ của Workspace.
* Bài đã bấm đăng nhưng chưa xác minh vẫn **giữ một suất dự phòng** cho tới khi được
  xác minh, nên hệ thống không gửi trùng. Bài thất bại trả lại suất.
* Hạn mứn được kiểm tra ở **backend** khi xếp lịch và cả khi đăng.
* Hạn mứn do người dùng đặt là quy tắc nội bộ, không phải ngưỡng do Facebook công bố.

### Chống đăng trùng

Bộ chống trùng lưu dấu vết của nội dung trong bảng `content_fingerprints`:

| Cấp | Cách nhận ra |
|---|---|
| Ảnh giống hệt | SHA256 của tệp |
| Ảnh gần giống | perceptual signature + khoảng cách Hamming |
| Caption | chuẩn hóa (bỏ dấu câu, hạ chữ thường) rồi băm |
| Dấu vết nội dung | kết hợp ảnh và caption |

Ngoài ra: không tạo hai job giống nhau cho cùng group, không cho nhiều Facebook đăng trùng
nội dung vào một group, không đăng lại bài đã bị từ chối, và không retry bài
`submitted_unverified`.

### Bảng mới

`pipeline_auto_post_settings`, `account_posting_limits`, `content_fingerprints`,
`posting_counters`. Cấu hình cũ được chuyển sang cấu hình chung khi nâng cấp.

### API

```
GET /api/pipelines/{id}/auto-post/settings
PUT /api/pipelines/{id}/auto-post/settings
GET /api/pipelines/{id}/auto-post/limits
PUT /api/pipelines/{id}/auto-post/limits/{account_id}
GET /api/pipelines/{id}/auto-post/dashboard
POST /api/pipelines/{id}/auto-post/distribute
```

## Lịch đăng thông minh (V9)

Một lịch chung cho toàn Pipeline: hệ thống tự phân bổ bài đều trong khung giờ
được phép, giữ khoảng cách giữa các bài, kiểm soát riêng từng Facebook và từng
group. Mỗi Facebook vẫn có hạn mức và lịch sử riêng.

Trong *Tự động đăng group* bấm nút **Lịch đăng** để mở phần cấu hình:

```json
{
  "mode": "spread",
  "timezone": "Asia/Ho_Chi_Minh",
  "start_time": "08:00",
  "end_time": "22:00",
  "min_interval_minutes": 90,
  "max_posts_per_account_per_day": 3,
  "days_enabled": [1, 2, 3, 4, 5, 6, 0]
}
```

Cấu hình này là quy tắc nội bộ của hệ thống, **không phải mức đăng an toàn do
Facebook công bố**.

### Thứ tự kiểm tra của bộ xếp lịch

1. Tài khoản đang hoạt động (bỏ qua tài khoản tạm dừng hoặc phiên đã hết hạn).
2. Hạn mứn bài mỗi ngày của tài khoản.
3. Khung giờ được phép và ngày trong tuần.
4. Khoảng cách tối thiểu giữa hai bài của cùng tài khoản.
5. Group đủ điều kiện và đã hết thời gian nghỉ.
6. Bài trùng lặp.
7. Kiểm tra an toàn của ảnh và caption.
8. Chỗ đứng thời gian hợp lệ.

Mỗi job luôn có sẵn tài khoản, group, ảnh và caption. Không đổi tài khoản lúc
chạy, không đăng hai bài cùng phút, không đăng trùng vào một group, không retry
bài `submitted_unverified` và không đăng lại bài bị từ chối hoặc bị gỡ.

### Module

| Module | Việc |
|---|---|
| `scheduler/time_windows.py` | khung giờ, ngày trong tuần, múi giờ, cách rải bài |
| `scheduler/account_limits.py` | hạn mức và khoảng cách của từng tài khoản |
| `scheduler/group_cooldowns.py` | khoảng nghỉ giữa hai lần đăng vào cùng group |
| `scheduler/job_allocator.py` | ghép thành lịch, ghi job idempotent, ghi lịch sử |

### Khung giờ qua nửa đêm

`22:00` đến `02:00` được hiểu là từ 22:00 hôm nay đến 02:00 hôm mai. Múi giờ
riêng của Pipeline được dùng cho mọi phép tính, không phụ thuộc giờ máy chủ.

### Lịch sử

Bảng `posting_schedule_history` ghi lại mỗi lần xếp lịch và lý do trì hoãn
(ví dụ "đã đủ 3/3 bài hôm nay", "chưa có group đủ điều kiện", "ngoài khung giờ").
Lịch đăng được lưu trong database nên không mất khi khởi động lại máy chủ.

### API

```
GET /api/pipelines/{id}/schedule
PUT /api/pipelines/{id}/schedule
GET /api/pipelines/{id}/schedule/preview
GET /api/pipelines/{id}/schedule/history
POST /api/pipelines/{id}/schedule/allocate
```

## Phân tích hiệu suất (Admin Analytics)

Chủ hệ thống xem hiệu suất của từng Pipeline và từng CTV ở `/#/analytics/pipeline/<id>`
và `/#/analytics/collaborator/<id>`. Trang tổng chỉ hiện số chính; bấm vào card Pipeline
để mở báo cáo chi tiết.

### Quy tắc của số liệu

* Chỉ số **chưa đo được** hiện "Chưa có dữ liệu", không bao giờ là 0.
* Tổng tương tác cộng **snapshot mới nhất** của mỗi bài, nên đo nhiều lần vẫn tính một lần.
* Lượt xem, reach và tương tác là ba nhóm số riêng, không cộng chung.
* Bài đã đăng, bài chờ duyệt, bài bị từ chối và bài bị gỡ là bốn trạng thái riêng.
* Quy kết hiệu suất cho CTV đã tạo bài; CTV duyệt bài và tài khoản Facebook đăng bài được
  ghi riêng trong `post_attribution`.

### Nguồn số liệu

| Nguồn | Tình trạng | Cần gì |
|---|---|---|
| Số bài đã đăng / chờ duyệt / bị từ chối | **Có sẵn** | không cần gì, đọc thẳng bảng `jobs` |
| Permalink của bài | **Có** sau khi bài mới đăng | worker ghi permalink khi bấm đăng |
| Reactions, comments, shares | **Chưa đo được** | cần `FB_GRAPH_TOKEN` (Graph API) hoặc bật `ANALYTICS_BROWSER_COLLECTOR=1` (đọc số hiển thị qua phiên Facebook) |
| Lượt xem reel, reach | **Chưa đo được** | Facebook không công bố hai số này cho bài trong Group và Reel trên trang cá nhân |

Bộ thu thập chạy nền mỗi `ANALYTICS_INTERVAL_MINUTES` phút (mặc định 180), mỗi bài chỉ có
một dòng trong hàng đợi nên chạy lại không làm trùng việc. Bộ thu thập không vượt CAPTCHA,
dừng ngay khi gặp checkpoint và không thu thập gì ngoài những gì tài khoản đã đăng.

### Cấu hình

```
FB_GRAPH_TOKEN=            # token Graph API chính thức (để trống nếu chỉ dùng phiên Facebook)
FB_GRAPH_VERSION=v21.0
ANALYTICS_INTERVAL_MINUTES=180
ANALYTICS_BATCH_SIZE=5
ANALYTICS_BROWSER_COLLECTOR=1
```

### API

```
GET /api/admin/analytics/overview
GET /api/admin/analytics/pipelines
GET /api/admin/analytics/pipelines/{id}
GET /api/admin/analytics/collaborators
GET /api/admin/analytics/collaborators/{id}
GET /api/admin/analytics/top-posts
GET /api/admin/analytics/timeseries
GET /api/analytics/me          # của CTV, chỉ Workspace và bài của chính họ
POST /api/admin/analytics/collect   # chạy bộ thu thập ngay
```

Các API `/api/admin/analytics/*` chỉ dành cho Chủ hệ thống. CTV cần quyền `analytics.view`
mới xem được `/api/analytics/me`, và chỉ thấy Workspace của mình cùng những bài mình tạo.

## Quy ước ID của Workspace trong giao diện

`useWorkspace` trả về `pipelineId` (số) lấy thẳng từ đường dẫn, nên mọi section luôn biết
Workspace hiện tại ngay từ lần render đầu. Trước đây các section dùng `workspace.pipeline?.id`
(lấy từ danh sách pipeline vừa tải xong) nên lần render đầu gửi đúng chuỗi `undefined` vào
`/api/admin/pipelines/undefined/key` — FastAPI trả 422 và giao diện hiện toast đỏ.

- Mọi request có guard: URL chứa `undefined`, `null` hay `NaN` bị chặn ngay ở `apiClient`.
- Backend có handler riêng cho `RequestValidationError`: trả về một câu tiếng Việt
  (`Dữ liệu gửi lên không hợp lệ: <trường>`), chi tiết kỹ thuật chỉ nằm trong log máy chủ.
- `tests/e2e_ui.py` theo dõi mọi response của API và mọi toast trong lúc duyệt các trang:
  một response 4xx/5xx (trừ 401 của phiên chưa đăng nhập) hoặc một toast màu đỏ là lỗi.

## Tài nguyên riêng của từng CTV (V6)

Mỗi tài nguyên trong Workspace mang theo chủ sở hữu (`owner_user_id`):

| Nguồn | Ai nhìn thấy |
|---|---|
| Admin tạo (tài khoản, group, ảnh, bài, task) | **Tất cả thành viên** của Workspace — đây là kho tài nguyên dùng chung |
| CTV tạo | **Chỉ chính CTV đó** và Chủ hệ thống |
| Admin xem | Tất cả tài nguyên của Workspace, kèm cột "Nguồn" (Của tôi / Chung) |

- Cột **NGUỒN** trong danh sách nhóm và tài khoản cho biết tài nguyên đó thuộc CTV nào hay thuộc kho chung.
- Hai CTV cùng Workspace **có thể cùng nhắm một group** với tài khoản Facebook riêng: khóa `UNIQUE(pipeline_id, group_key)` đã được đổi thành khóa theo chủ sở hữu (`pipeline_id, owner_user_id, group_key`).
- Khi CTV truy cập tài nguyên của CTV khác, API trả **404 Not found** — không rò rỉ dữ liệu.
- Quyền mặc định của CTV đã gồm `facebook.add` và `groups.manage`, nên CTV tự thêm được tài khoản và group của mình. Chủ hệ thống có thể thu hồi từng quyền trong màn "CTV & phân quyền".
- Khi xóa một CTV, tài nguyên riêng của họ **không bị xóa ngay** mà chuyển về kho chung của Workspace (để không mất dữ liệu); Chủ hệ thống xem lại và dọn thủ công nếu cần.
- Dữ liệu cũ của Workspace hiện có nằm trong kho chung, nên mọi CTV vẫn dùng được như trước.

## Giao diện React (frontend/)

Giao diện được viết lại bằng React 18 + TypeScript + Vite. Mã nguồn nằm ở `frontend/`, sản phẩm build được phục vụ từ `static/` bởi chính FastAPI (mount `/static`). Backend, cookie phiên, CSRF và RBAC không thay đổi.

```bash
cd frontend
npm install          # chỉ cần khi phát triển, VPS chạy production không cần Node
npm run build        # typecheck + build vào frontend/dist
```

Deploy (build xong rồi đưa artifact sang `static/` và nạp lại dịch vụ):

```bash
cd /root/facebook-pipeline-manager
./deploy-ui.sh
```

`deploy-ui.sh` chạy `npm run build`, đồng bộ `frontend/dist` sang `static/`, `systemctl restart fpm.service` và kiểm tra `/` cùng các asset trả về 200.

### Đường dẫn và điều hướng

Ứng dụng dùng **HashRouter** (`#/workspace/1/accounts`) nên một tải lại trang không bao giờ gặp 404 từ máy chủ; đường dẫn cũ đã lưu trong bookmark vẫn dùng được. Backend vẫn chỉ phục vụ `static/index.html` tại `/`.

### Rollback

Bản giao diện cũ (một file HTML tự chứa) được giữ tại `/tmp/kilo/index.legacy.html`. Nếu cần quay lại:

```bash
cp /tmp/kilo/index.legacy.html static/index.html
systemctl restart fpm.service
```

Bản legacy không tham chiếu `/static/assets` nên việc rollback không cần Node hay `npm install`.

### Kiểm thử giao diện

```bash
cd /root/facebook-pipeline-manager
.venv/bin/python -m pytest -q tests            # gồm 2 test dùng trình duyệt thật
.venv/bin/python tests/e2e_ui.py http://127.0.0.1:9088
```

`tests/e2e_ui.py` mở trình duyệt ở 7 kích thước màn hình (390, 402, 430, 768, 834, 1280, 1440 px), đăng nhập, đi qua từng mục của workspace và báo lỗi khi: có console error, request thất bại, tràn ngang, chữ bị dồn thành cột hẹp, hoặc checkbox nằm xa nhãn.
