# Local configuration

Normal [app.py](../app.py) uses `data/` beside the entry file. It restores a stopped
candidate, never an active session, Batch, counters or current result. Historical
records remain separately browsable without restoring application state. No first-file
fallback, automatic repair of saved geometry or fake inference is used for recovery.

## Files and paths

| File | Content |
| --- | --- |
| `data/app.json` | optional `last_applied_recipe_id`, `language` (codes below) |
| `data/communication.json` | optional global Production reporting, diagnostic endpoint and Web preferences |
| `data/history.sqlite3` | generated internal SQLite Production history; not operator configuration |
| `data/security.sqlite3` | optional local accounts and versioned password verifiers; independent of History |
| `data/security.lock` | application/offline-maintenance ownership lock; file presence does not mean it is held |
| `data/models/<model_id>/model.json` | Model definition: `display_name`, `expected_sha256`, `module_id` |
| `data/models/<model_id>/model.vimosln` | Managed Model artifact, verified by SHA-256 |
| `data/models/<model_id>/vimo_trt_cache/` | SDK-owned generated TensorRT artifacts |
| `data/recipes/<recipe_id>.json` | Recipe with an optional direct `model_id` |
| `data/machine.tsv` | 30/36 explicit ROI rows keyed by ChannelId; fixed camera/slot mapping |
| `data/camera_sources.json` | Folder/Basler source mapping, Loop and replay FPS |

Each managed Model owns its fixed `model.vimosln` beside `model.json`. Folder
paths resolve relative to `data/camera_sources.json` (the `data/` directory). The
public engine still accepts arbitrary model paths; root `models/` is a separate
engineering input area. Normal startup does not substitute it for `data/models/`.
Model SHA-256 is checked during preparation and again on the SDK owner before
native initialization. Artifact work stays off Qt dispatch. Managed Production
and Prepare TensorRT cache directly load the same authoritative
`data/models/<model_id>/model.vimosln`; the SDK generates/reuses adjacent
`vimo_trt_cache/`, without another runtime model copy. Model content and
`model.json` remain unchanged. Normal SDK TEMP provider/timing-cache writes are
permitted and do not affect Model identity or configuration readiness.
Explicit-path engineering tools retain their default `outputs/vimo_cache/<sha256>/`;
old output caches are neither migrated nor removed automatically.
Obsolete managed `cache/<sha256>/` staging trees are not used or automatically
migrated/deleted.

The five JSON definition families are versionless; `schema_version` and obsolete aliases
are unknown fields, not migration inputs. Nothing automatically rewrites existing
files. Parsers reject unknown/duplicate fields, invalid present values, duplicate
IDs and malformed hashes. Incomplete definitions may be saved where documented;
preparation and runtime reject unresolved references. Integers reject booleans/floats. IDs
and labels are exact case-sensitive text, not trimmed aliases. Hashes normalize
to lowercase. Model discovery visits immediate child directories only, reading
`model.json` in each; root-level files and deeper descendants are not managed
entries. Recipe discovery reads immediate `*.json` files. Missing directories
are empty catalogs and discovery creates nothing. Malformed/unreadable entries
remain visible with their paths; unrelated valid entries remain usable. Identities that collide under casefold are all excluded with diagnostics, never
chosen arbitrarily.
Only the selected Recipe and its actual Model/layout/source dependencies govern
candidate readiness; unrelated catalog issues are nonblocking. Missing required data
keeps the shell UNCONFIGURED; a structurally valid candidate with unavailable
Folder inputs can remain READY with a warning but fails Start preflight.
Unreliable saved preferences are not silently overwritten. Invalid optional language
can fall back visibly; required Model/Recipe identities cannot be guessed.

`app.json` may be absent or `{}`: the language defaults to `zh` and no Recipe or
Model is selected. Its only fields are optional `last_applied_recipe_id` (nonempty
text or null) and `language`; there is no global/default Model. Startup never
chooses the first Recipe/Model. A selected Recipe resolves its own exact `model_id`.
Missing preferences do not block the shell or available configuration editors;
other required data still governs editor availability and Production readiness.
Only explicit language changes or successful Production candidate installation
write preferences. Vision Save never changes the last-applied Recipe preference.
Malformed files remain unchanged, and unknown fields are errors. An unsupported
language retains the existing visible, in-memory `zh` fallback with writes blocked
until repair; it is never silently persisted.

Language identities, validation, native names and display ordering belong to the
Qt-independent `localization/languages.py`. Static base text/resolution and authored
translations live in `localization/catalog.py` and `localization/translations.py`.
Configuration preferences persist only the selected code and import that identity
contract; visual styling and widgets consume Localization directly. Missing optional
text uses canonical English; an unknown key fails before fallback.

UI language codes are exact: `zh` (Simplified Chinese), `zh-TW` (Traditional Chinese),
`en` (English), `ja` (Japanese),
`ko` (Korean), `es` (Spanish), `pt-BR` (Brazilian Portuguese), `it` (Italian),
`id` (Indonesian), `fr` (French), `de` (German), `ru` (Russian), `tr` (Turkish),
`nl` (Dutch), `pl` (Polish), `uk` (Ukrainian), `ro` (Romanian), `cs` (Czech),
`hu` (Hungarian), `sv` (Swedish), `da` (Danish), `fi` (Finnish),
`nb` (Norwegian Bokmål), `sk` (Slovak), `bg` (Bulgarian), `hr` (Croatian),
`sr-Latn` (Serbian Latin), `el` (Greek), `pt-PT` (European Portuguese),
`vi` (Vietnamese), `ms` (Malay), `fil` (Filipino), `sw` (Swahili),
`th` (Thai), `hi` (Hindi), `bn` (Bengali), `ta` (Tamil), `te` (Telugu),
`mr` (Marathi), `ne` (Nepali), `si` (Sinhala), `am` (Amharic), `km` (Khmer),
`lt` (Lithuanian), `lv` (Latvian), `et` (Estonian), `sl` (Slovenian),
`sq` (Albanian), `ca` (Catalan), `af` (Afrikaans), `zu` (Zulu),
`ha` (Hausa, Latin/Boko), `kk` (Kazakh Cyrillic), `uz` (Uzbek Latin),
`mn` (Mongolian Cyrillic), `so` (Somali Latin), `tk` (Turkmen Latin),
`tg` (Tajik Cyrillic), `az-Latn` (Azerbaijani Latin), `mg` (Malagasy Latin),
`ky` (Kyrgyz Cyrillic), `ka` (Georgian), `hy` (Armenian) and `ti` (Tigrinya, Ethiopic).
Tigrinya has an independent catalog, not Amharic translations.
Afrikaans and Zulu use Latin script;
Hausa preserves ɓ/ɗ/ƙ and Uzbek uses oʻ/gʻ. Alternative scripts are not aliases.
`az-Latn` supports Latin-script Azerbaijani only; `az` and other scripts are not aliases.
The collapsed selector uses compact language labels; its popup shows full native
names in a single column that expands to fit all 64 languages when screen height
permits. On shorter screens the popup stays within the available work area and
provides scrolling. Display order pins `zh`, `zh-TW`, `en`, then sorts Latin-script
native names case/accent-insensitively. Greek, Cyrillic, Armenian, Georgian,
Japanese, Korean, Thai, Devanagari, Bengali, Tamil, Telugu, Sinhala, Ethiopic and Khmer groups follow;
native-name order is stable within groups.
This presentation order is independent of saved codes and does not change with
the active language or theme. `pt-PT` and `pt-BR` are distinct.
It saves only the existing language preference;
switching does not save configuration drafts or change an active inspection.
Existing `zh`/`en` and omitted-language files remain compatible. Unknown codes
fall back to Chinese in memory with a warning, preserving the file for repair.
Optional untranslated text uses English. Technical identifiers, model labels and
reason codes are preserved. Visible captions, short states, confirmations and
icon-only action names remain localized. Routine explanations that repeat labels,
states or other controls are omitted. Tooltips identify actions, explain essential
operation limits, or retain elided identities and specific operator-recovery
information. Routine runtime telemetry and diagnostic expansions are omitted.
Layout is left-to-right; the supported minimum is
1080 Qt logical pixels. Windows must provide fonts for the selected script,
including Georgian, Armenian, Vietnamese marks, Thai, Indic scripts, Sinhala,
Ethiopic and Khmer.
Qt uses existing Windows font fallback (such as Nirmala UI for Indic scripts and
Sinhala, Ebrima for Ethiopic, Tahoma for Thai and Leelawadee UI for Khmer); fonts
are not downloaded, bundled or converted at runtime.

Normal GUI startup suppresses Info messages in exactly `qt.text.font.db` through
Qt's native logging rules; candidate-font rejection messages do not establish
missing glyphs. Warning/Critical/Fatal and other categories retain their normal
behavior and logging destinations. Explicit `QT_LOGGING_CONF` and
`QT_LOGGING_RULES` settings retain Qt's precedence over this application rule.

## Local accounts

Normal startup owns the Qt-independent `LocalIdentity` service for entry-relative
`data/security.sqlite3`. Every process starts as anonymous Guest; no login is
restored. Guest has no account row. A missing database remains missing and does not
block Guest startup. The companion `security.lock` is acquired even when the
database is absent and retained until the identity worker actually exits on Close.
Other cooperating application/maintenance owners for the same canonical path are
rejected. The lock file is retained; do not delete it to clear a live owner.

Accounts have immutable UUID identities and usernames, an enabled flag and one of
Operator, Engineer or Administrator. Roles form the fixed hierarchy Guest <
Operator < Engineer < Administrator. Usernames are 1–64 printable characters without
edge whitespace; uniqueness uses Unicode `casefold`, independently of SQLite's
ASCII-only NOCASE. No username renaming or account deletion is provided.

Schema version 2 adds nullable `last_login_utc` account metadata. Opening a valid
version 1 store validates its schema and records before migrating it in one SQLite
write transaction; failed migration rolls back. IDs, usernames, roles, enabled
flags and password verifiers are preserved. New stores use version 2 directly.
The verifier remains PBKDF2-HMAC-SHA256 version 1, with a random 16-byte salt,
32-byte digest and iteration count. New passwords use 600,000 iterations; supported
stored counts are bounded to 100,000–2,000,000. Passwords are nonempty UTF-8 text,
at most 1024 encoded bytes. Verification uses constant-time digest comparison.
Plaintext passwords are not persisted, returned in observations or logged.

Successful authentication rechecks its attempt identity and session revision,
commits Last Login as UTC ISO 8601 text, and then publishes the immutable
account/session observation with a new revision, including same-account
reauthentication. A timestamp write failure cannot produce a successful login.
Failed, canceled or superseded authentication, Guest, logout, password changes/resets
and account management do not update this value. The UI displays device-local
`YYYY-MM-DD HH:mm`, or an em dash for an account that has never logged in. This
metadata is not an audit trail.

Administrator authorization is enforced by the account service for account listing,
creation, enable/disable, role changes and password resets. The last enabled
Administrator cannot be disabled or demoted; this check and the update share one
SQLite write transaction. Changes to the current account are published before the
operation completes: role changes take effect, disabling or administrative password
reset returns to Guest, and successful self-password change can retain login.
All session observations are immutable and carry monotonically increasing
revisions within that owner lifetime. A restart starts a new Guest revision sequence.
A new authentication attempt supersedes an older pending attempt without changing
the active session or its revision. Ordinary authentication rejection (including
wrong credentials, unknown names and disabled target accounts) and cancellation
preserve the otherwise-valid current session. Logout, committed login, current-account
role/enablement/password changes and Close invalidate pending authentication and
uncommitted account-management requests from an older session decision. Cancellation
and successful login are ordered by the service commit lock: canceling a specific
pending attempt prevents its Last Login write, but cannot undo a login that already
committed. Accepted worker requests still drain to terminal completion.

Corrupt, unsupported and unwritable stores are never silently replaced. Normal
startup exposes the unavailable storage observation and `Startup.security_error`,
and the launcher prints the error to stderr; Guest has no account privileges.
Failure to acquire the ownership lock rejects startup/maintenance explicitly.
There is no automatic database repair, account creation or privileged fallback.
Runtime security-storage failure returns the session to Guest; dismissing a form
does not restore an earlier session snapshot.
SQLite account data has its own schema/application identity, no History retention
and no audit/event storage. Local filesystem access and the maintenance environment
remain OS-level trust boundaries.

First Administrator initialization and credential recovery are explicit offline
commands. Stop the application first, select the directory containing its entry
file, and enter/confirm the secret interactively:

```powershell
uv run --no-sync python -m capsule_vision.application.account_maintenance --entry-directory '<entry-directory>' initialize-admin '<username>'
uv run --no-sync python -m capsule_vision.application.account_maintenance --entry-directory '<entry-directory>' recover-admin '<username>'
```

Initialization requires an empty account store. Recovery resets and enables an
existing Administrator, preserving its ID, username and all other accounts; it
cannot promote an Operator/Engineer. Both operations acquire the same exclusive
ownership lock before accessing storage. Password arguments, noninteractive secret
input and echoing console fallbacks are rejected. Interrupted/failed initialization
is reported for explicit review, not silently repaired.

The navigation's current-user button uses the exact caption `Guest` for anonymous
sessions in every language, or the account's original username when logged in.
Its tooltip and accessible name retain the localized role and any storage status,
without repeating the anonymous identity. Role names elsewhere remain localized.
Login and Switch User use masked credential forms. Opening, canceling or failing
Switch User preserves the otherwise-valid current login; only committed successful
authentication replaces it. Pending dismissal invalidates that specific attempt
in the account service as well as its UI callback. Logout immediately invalidates
pending authentication.
Change Password verifies the current password and confirms the new one. The
Administrator's resizable Manage Users dialog prefers 850×500 Qt logical pixels,
bounded by the available screen work area on first display. Its read-only,
single-selection table allocates its viewport width to Username / Role / Status /
Last Login in 30% / 22% / 16% / 32% proportions. Shared column edges round to logical
pixels and use the entire viewport, including after scrollbar changes. Widths
respond to viewport, language and font changes, independently of account text and
selection. Headers and cells align left; long text elides. Account count changes
do not resize the dialog. Disabled accounts remain visible,
additional accounts scroll, and refresh preserves selection by account ID. Full
cell values remain available through tooltips and accessibility.
All actions sit below the table: New User and icon-only Refresh on the left;
Change Role, Enable/Disable User and Reset Password on the right. Ordinary fonts
use one row; constrained widths or enlarged fonts wrap the groups without
overlapping controls. There is no separate selected-account summary or Close button.
Change Role opens a separate role selector with Save/Cancel; unchanged roles
cannot be saved. Disabling requires a confirmation naming the target; enabling
does not. Demotion/disable controls for the last enabled Administrator are disabled
as a UI convenience, alongside the authoritative transactional service guard.
Account creation and password reset retain the existing masked forms. Every action
uses the account service's own authorization and validation; there is no delete or
username rename action. Native Close and Esc dismiss the dialog. Dismissal fences
late UI delivery without cancelling already accepted service work or redirecting
it to another selection. Application Close retains its session-revision and actual
worker cleanup rules.
Current-account role changes update the menu; disable/reset returns it to Guest.
Account dialogs close when their actor loses the required identity/role. Close
invalidates UI delivery and waits for the existing Application-owned worker cleanup.
Missing/unavailable storage is visible in the current-user tooltip and login form;
offline initialization/recovery remains required. Identity menus, roles, forms,
account management and feedback have native catalog entries in all 64 supported
languages. Open identity dialogs refresh their captions without losing entered
values or selected role identities. Unrelated optional UI entries retain their
English fallback. Passwords are cleared from
fields on submission/dismissal; feedback never echoes credentials or raw errors.

Normal startup restores the saved Production candidate and reporting selection as
Guest. Operator or higher can select or re-prepare a Recipe, set Batch, Start,
Reset Fault and connect or disconnect configured Basler sources. Changing automatic
PLC reporting requires Engineer or higher. Guest can Start or single-step from
READY only with a current prepared Folder Replay candidate and reporting disabled.
Only a generation admitted under that Guest exception permits Guest Pause, Resume
or Step while running. All roles can observe, Stop and Close. Identity changes leave
accepted operations running and invalidate pending interactive Recipe selections.

Engineer or Administrator can save or delete Recipes, save MachineLayout and
CameraSources, inspect and publish Model imports, rename or delete Models, and
prepare their caches. Reading saved definitions and viewing existing status is
available to every role, including the selected Model's complete label catalog.
Device enumeration and new tuning measurements require Engineer. Passive
capture on an existing connection keeps its Operator admission.

All five pages remain available in every session. On the Recipe page, Guest
and Operator browse saved definitions. The displayed metadata, inspection
settings, Model and label selections follow the chosen saved Recipe. Refresh
reads the catalog again. Browsing leaves the engineering entry, working draft,
Production candidate and last-applied preference intact. Engineer and
Administrator return to the working draft, including any unsaved edits.
Production's saved-Recipe selector is a separate Operator command.

Production and Vision combine the current role with lifecycle, source readiness
and editing constraints. Guest player controls follow the admitted replay
generation. Stop and application Close remain available under their normal
lifecycle rules. Vision reference images and configuration remain viewable
while engineering mutations are disabled.

Vision checks each file publication separately. If the session changes between
saves, the first successful file remains saved and the other draft remains
available for review. A staged Model belongs to its initiating session revision.
Cancelling the import can remove its staging directory after logout.

## History storage

Normal startup creates the history owner independently of configuration readiness.
Both Folder and Basler terminal Production results are eligible. The database
uses internal schema version 1 and may have SQLite `-wal`/`-shm` sidecars. Preserve
these files with other local assets; do not copy an active database without its
SQLite consistency requirements. An unsupported schema, corrupt database or
unwritable path is reported as unavailable, never silently reset or replaced.

New records capture the active resolved Recipe's display name alongside its stable
ID in the compact detail payload. Renaming a Recipe does not relabel past cycles.
The current detail payload contains channel evidence, camera evidence and an
optional Recipe name. Exactly three elements are required; former two-element
payloads are unsupported and follow the existing corrupt-record query error
behavior. A current null name remains valid and displays the existing missing-name
Recipe ID presentation. History never looks up a current file to invent a name.

Records expire incrementally after 30 days by their host completion observation,
not camera exposure time. This does not remove archived images, link filenames or
restore Batch labels/counters on launch. History has no retention settings or JSON
preferences. In-memory diagnostic/fake compositions may omit persistence. See
[ownership and bounds](architecture.md#production-history) for shutdown, loss
observations and query limits.

## Communication preferences

`data/communication.json` is independent from `app.json`, Recipes, MachineLayout
and CameraSources. Communication's explicit **Save settings** atomically replaces
this file (same-directory temporary write, fsync, replace); it does not change any
other asset or start a network owner. Network/reporting fields are drafts until a
successful Save. The Web header's power action is the specific exception: it
persists only `web.enabled` from the actual validated file, then requests runtime
start/stop using saved Web network settings. It never saves unrelated drafts or
changes effective reporting selection. An invalid file is not silently replaced;
first-use defaults can establish a missing file. Unsaved reporting edits never change next-Start admission; diagnostic
requests continue to use the effective saved endpoint, not draft text. Save and
reporting publication are serialized with Start under the existing Application
lock. An unchanged reporting selection permits saving independent Web/diagnostic
preferences during Production. Active sessions keep their frozen settings.

Confirmed Discard reloads the actual file through this codec, without writing or
undoing requests/results. On any reload/validation failure (including a missing
file before the first Save), the complete draft is preserved. Default settings
can be explicitly saved to establish that initial file. Save/Discard feedback
and actions remain outside the scrolling content.

The versionless object permits optional `reporting`, `diagnostic` and `web`
sections; an existing `{}` means legacy UDP defaults, with reporting and Web
disabled. Unknown or duplicate fields and invalid present types/ranges are rejected. Superseded
`endpoint` envelopes, `transport` fields and protocol aliases are not accepted;
startup reports them without rewriting the file. Explicit Save writes this format.

The codecs construct the same immutable protocol-specific endpoint values for
Diagnostics and reporting. Reporting's address/capacity belong to its separate
in-memory settings; this does not add an endpoint envelope to the saved format.

Reporting and Diagnostics are flat endpoint objects. `protocol` is `fins_udp`
(omitted default) or `fins_tcp`; `host` defaults to `127.0.0.1`, `port` to 9600
(1–65535), and `timeout` to 1 second (0.05–30). UDP alone permits `destination`
and `source` objects: defaults are network/node/unit 0/2/0 and 0/1/0 respectively;
individual routing fields may be omitted, with existing FINS ranges enforced.
TCP rejects persisted UDP routing fields and negotiates its nodes internally.
Switching to TCP and saving drops those fields, without retaining a hidden route.

Both sections also accept `protocol: "modbus_tcp"`. Its optional `host`,
`port`, `timeout` and `unit_id` default to `127.0.0.1`, 502, 1 second and 255.
Unit ID must be an integer 0–255; port/timeout retain the ranges above. FINS
`source`/`destination` and all unknown fields are rejected for Modbus. Diagnostics
rejects reporting-only `base_address`/`enabled`. A diagnostic endpoint never
enables reporting or shares its Unit ID implicitly. Explicit Save removes fields
inapplicable to the selected protocol. For example, default port and
Unit ID are omitted:

```json
{"diagnostic": {"protocol": "modbus_tcp", "host": "192.168.1.20"}}
```

Holding Register addresses are zero-based, independent of FINS DM/WR. Watch
groups remain session-only and are never implicitly converted by a protocol edit.

Reporting additionally permits `enabled` (default false), `base_address` and
`mode`. The only values are `per_cycle` and `four_cycle`; omitted `mode` in
existing documents retains `per_cycle`. Four-cycle mode is saved explicitly and
requires `protocol: "fins_tcp"` with base 49 or 149, even when disabled. Enabled
four-cycle Start additionally requires Basler. Counter reads use base−1, bitmap
writes base+1 and commit writes base. Selecting this mode never enables reporting
or replaces an endpoint/address automatically. Obsolete `w48_observe` and
`w48_active` values are invalid: no compatibility interpretation, migration or
file replacement occurs on load. Replacement is an explicit stopped-environment
maintenance/recovery action. See the [reporting contract](communication.md#four-cycle-reporting)
for attribution, register ownership and field acceptance.

Per-cycle FINS defaults to 49 (integer 0–508, WR area). Modbus has **no default base**:
it must be explicitly supplied as an integer 0–65532 whenever reporting is enabled.
A disabled Modbus definition may omit it for later completion. Present null,
invalid types and out-of-range bases are rejected. Save always retains an explicit
Modbus base, including 49; it is never interpreted as the FINS default.
Sequence is at base; bitmap words start at
base+1. A 30-channel session writes two bitmap words and never base+3; 36 channels
writes three. The four-register window must fit the protocol's address space.
Modbus initializes through FC03, writes bitmaps through FC16, and commits through
FC06 only after validated bitmap ACK and attribution. Save only changes
future Starts; active settings/address range stay frozen. Queue limits and channel
bit mapping are not settings. Defaults retain W49–W52 behavior.

For example, an enabled Modbus reporting selection with default port/timeout/Unit
ID serializes as:

```json
{"reporting": {"protocol": "modbus_tcp", "enabled": true, "base_address": 100}}
```

Save omits fields and whole sections equal to the persisted schema's legacy
defaults, independently of initial in-memory defaults. Newly saved FINS/TCP
reporting and diagnostic sections always contain `"protocol": "fins_tcp"`, even
when disabled and otherwise unchanged. Nondefault settings remain
even when a service is disabled; endpoint sections written by Save identify their
protocol explicitly. For example:

```json
{
  "reporting": {
    "protocol": "fins_tcp",
    "host": "192.168.251.1",
    "enabled": true,
    "base_address": 120
  },
  "diagnostic": {
    "protocol": "fins_udp",
    "destination": {"node": 7}
  },
  "web": {"enabled": true}
}
```

`web` permits `enabled` (default false), `host` (127.0.0.1), `port` (8765) and
`interval_ms` (500 default; 500/1000/2000). Only
127.0.0.1 or a specific unicast IPv4 is supported. Saving/reloading preserves
unavailable adapter addresses; actual service startup checks local assignment and
subnet validity. There is no DNS, wildcard fallback or automatic IP substitution.

A missing file leaves reporting and Web disabled, without blocking valid
Production startup or creating a file. Communication shows **Defaults · not saved**;
reporting initially selects `four_cycle`, FINS/TCP and W49, disabled. Diagnostics
also initially uses FINS/TCP at 127.0.0.1:9600 with a one-second timeout, as does
the reporting endpoint. Explicit engineering launchers retain their per-cycle
defaults. Web remains 127.0.0.1:8765 at 500 ms. These are editing defaults,
not a saved Production destination. Camera source mode never selects a PLC endpoint.
An existing empty or partially populated document still decodes omitted endpoint
sections/fields as FINS/UDP; explicit UDP, TCP and Modbus selections are unchanged.
No PLC connection or reporting activity starts merely from loading initial values.
Explicit Save establishes the file; Discard cannot reload a nonexistent file and
preserves the draft with feedback. Valid files restore their exact saved
settings, including Web auto-start when enabled, without rewriting them.
Invalid files remain unchanged, with visible recovery
feedback; reporting and Web stay disabled in memory until reviewed. An explicit
Save replaces the invalid file with validated settings. If a saved Web address is
no longer assigned or its port cannot bind, optional service startup fails visibly
without changing the file or invalidating Production readiness.

Restored reporting selection is frozen at the next Production Start, never a
connection or authorization to transmit at startup/warm-up. Runtime observations,
ACKs, watches and current counters are not persisted as Communication settings.
Terminal Production results have separate History storage. Saving does not alter the
running Web service. The header power action changes saved enablement; a failed
write prevents runtime action, while failed startup leaves saved enabled distinct
from actual FAILED. Apply changes is available only for differing saved/running
settings, after Web network drafts are saved/discarded. It uses the existing safe
restart path. The next process launch restores saved enablement. Runtime settings
and URL are presented separately from edited/saved values.

## MachineLayout

`data/machine.tsv` is UTF-8 tab-separated text with exactly this header:

```text
channel	x	y	width	height
1	0	0	176	960
```

The example shows one row; a valid table requires exactly 30 or 36 data rows.
Channels must be unique and contiguous 1..30 or 1..36. All five cells are unsigned
ASCII decimal integers; width/height and ChannelId are positive. Headers, column
counts, malformed numbers, blank data rows, duplicate/missing/out-of-range channels
and invalid ROI geometry are rejected. UTF-8 with or without BOM is accepted.
Reads use explicit ChannelIds regardless of row order; Save writes ascending IDs.
The resulting immutable layout is canonical CAM1..CAM5/CAM6 with six slots each:
`camera_index = (channel - 1) // 6 + 1`, `slot = (channel - 1) % 6` and
`camera_id = CAM{camera_index}`. CameraId and slot are not separately persisted.
An in-memory machine definition with a contradictory mapping/order is invalid;
mirrored spatial arrangements use explicit ROI coordinates without remapping IDs.
There is no JSON MachineLayout loader or fallback to `data/machine/machine.json`.

ROI origins are nonnegative integers and sizes positive integers in original
image coordinates. Overlap is allowed; actual reference/input dimensions supply
bounds validation. No silent clamping is performed. Saved MachineLayout is the
sole persisted geometry authority: Recipes cannot override it, and opening or
refreshing Vision never replaces or repairs saved geometry.

With no saved MachineLayout, Vision starts with six editable source placeholders
and no ROI geometry. CAM1–CAM5 are required; removing/restoring CAM6 selects 30/36
channels. Each new camera initializes once from actual Folder/imported/captured
pixels or observed connected dimensions: boundaries `floor(i*W/6)`, i=0..6,
partition the full width, with y=0 and height H. Width must be at least six pixels.
Without dimensions, geometry stays uninitialized. This is an editable template,
not calibrated geometry; changing references never replaces initialized ROIs.

Vision keeps six configuration positions. An unconfigured CAM6 is not an offline
configured camera. While stopped and disconnected, its minus action confirms
removing CAM6 geometry/source from the draft; explicit Save persists the edit.
Production re-selection prepares the changed camera set. No automatic backup is
created. On an existing saved machine, its plus
action copies only the current CAM5 draft's six ROI rectangles into independent
CAM6 values, preserving slots 0–5
and assigning CH31–CH36. CAM1–CAM5 are unchanged; CAM6's Folder path and Basler
serial remain empty. This is an editable starting template, not verified calibration;
actual CAM6 image/dimension bounds are still validated without clamping. Nothing is
persisted or activated by + alone. Before the first MachineLayout Save, restoring
CAM6 instead leaves geometry uninitialized until its own dimensions are known.
Incompatible channel numbering on removal fails instead of being remapped.
MachineLayout count changes do not depend on unrelated Recipes or Model resolution.
Production preparation still resolves the selected Recipe's Model and verifies its artifact
before replacing the candidate.
Discard reloads actual persisted files, including after partial multi-file saves;
inconsistent saved MachineLayout/CameraSources fail without replacing the draft.

## Model identity and storage

A managed Model's directory name is its sole persistent ID. `model.json` requires
exactly `display_name`, `expected_sha256` (64 hexadecimal digits) and `module_id`.
There are no persisted `model_id` or `model_path` fields. Loading constructs the
existing typed Model identity and absolute artifact path to the owning directory's
`model.vimosln`; missing artifacts/SHA mismatches still fail preparation. Production
preparation reads current saved Model metadata independently of the editor registry.
Recipe catalog Refresh discovers Model/Recipe metadata without choosing a first
entry. Discovery never initializes an engine, copies an artifact or creates a cache. Explicit-path
tools accept any `<model_id>/model.json` using this same contract, without requiring
the normal application's `data/` root. They do not accept the old JSON schema.

Model/Recipe IDs are exact safe single filename components, independent of display
names. Separators, traversal, Windows reserved/invalid names, control characters,
leading/trailing whitespace and trailing dots are rejected, not trimmed or repaired.
Case variants are not aliases; ambiguous casefold collisions cannot be selected.
Metadata-file symlinks and managed Model directory symlinks are rejected.

Recipe's Model row offers Import, Rename and Delete, alongside Prepare TensorRT
cache and its session-only result observation. File-import and acceleration icons
keep those actions distinct; labels remain available through tooltips/accessibility.
Import reads supported segmentation metadata through the SDK adapter without an
engine/GPU module, on the existing background IO slot. It hashes/copies into a
private sibling staging directory, verifies the copy and requires a module choice
when several segmentation modules exist. A single module is selected automatically.
Only a complete validated directory is published. Cancel/failure cleans the owned
unpublished copy; failed cleanup reports its path rather than claiming success.
Import makes the Model selectable without changing an unsaved Recipe or applying it.

New Model IDs use the first 12 lowercase SHA-256 digits. Duplicate full digests or
identical managed artifact bytes are rejected regardless of old ID/module. An
occupied prefix (including invalid entries) extends only the new ID to 16, 20,
etc. digits. Existing IDs/directories are never renamed or overwritten.
Rename changes only `display_name`. Delete requires confirmation, no saved Recipe
reference and no candidate/active reference or unfinished native owner. Deletion
checks each Recipe JSON's root `model_id` independently of full Recipe validation:
an exact matching string blocks deletion even if other fields are invalid;
a different string, absent field or null does not. Unrelated field errors do not
block deletion when that reference is unambiguous. Unreadable/unparseable files,
non-object roots, invalid reference types and duplicate JSON keys block deletion
with the Recipe path reported. All reference checks finish before any Model or
cache removal. Normal catalog discovery and full Recipe validation are unchanged.
Deletion removes only that managed Model directory, including its own cache; the
external imported source and other Models are untouched.

There is no cache inspection or clear action. Cache file presence does not establish
successful initialization. Model file operations reject live native ownership;
Start/preparation cannot race admitted management work, and Close waits for its
actual completion. Recursive
operations reject symlinks/junctions/external paths instead of traversing them.
New management tooltips/confirmations have Chinese/English resources; other
languages use the existing optional English fallback. Existing translations remain unchanged.

## Recipe

The `<recipe_id>.json` filename stem is its sole persisted identity; `recipe_id`
is not a JSON field. Only `display_name` is required. Optional fields are `model_id`,
`product_code`, `defaults`, `capsule` and
`image_archive`. A nonempty `model_id` selects that exact Model; there is no global
Model, inheritance or first-Model fallback. Save validates the Recipe's structure
and persistence identity, so a missing or unresolved Model reference can remain
saved. Production preparation rejects it without replacing the current candidate
or active session. Referenced artifacts must still pass path/SHA/module validation.
Recipe Save persists only the definition. Production selection separately prepares
it; preparation failure keeps the saved definition available for completion.
`camera_overrides` and `channel_overrides` are unknown fields and are rejected,
including empty arrays. They are never ignored or migrated.
Renaming changes only `display_name`, never the ID/path. Save writes the unchanged
selected target; New/Duplicate creates a separate Recipe identity.
New/Duplicate drafts propose `YYYYMMDD-XXXXXXXX` IDs using the local creation date
and eight lowercase Base32 characters from secure random bytes. Case-insensitive
collisions allocate a new suffix. First Save atomically publishes a complete file
without replacement; a late identity collision reallocates only the still-uncreated
generated object. Other IO errors are not retried. Once saved, identity remains
stable across renaming, Model/parameter edits and later Saves. No identity sidecar
is written; old valid Model/Recipe IDs remain supported without migration.
External definition changes, invalid/ambiguous target files and identity conflicts
require explicit reload/repair. Invalid unrelated files do not block Save,
preparation or Delete. Explicit Save may create a missing Recipe directory; atomic
replacement still uses a closed, flushed/fsynced same-directory temporary file.
Delete protects current candidate/active and persisted applied identities and
refuses an externally changed selection. No operation implicitly selects another
Recipe or updates an active session.

Default-valued optional groups/fields are omitted on explicit Save. For example,
`data/recipes/sample.json` containing `{"display_name":"Sample"}` is a complete
saved definition but cannot be prepared without selecting a Model. Default ViMo settings are enabled
and required with no exclusions; Size/Color and archiving default to disabled.
Changed settings remain explicit even when their check is disabled.

Recipe `defaults.vimo` defines one shared `enabled`, `required` and `disabled_labels`
policy for every configured camera/channel. Missing/null leaves use the effective
defaults: enabled and required, with an empty filter. The structural objects cannot
be null. Exclusions use exact case-sensitive label identities; an empty list excludes
nothing. Each channel retains its own image, ROI, identity, findings and judgment.
Filtering changes consumed findings, not model thresholds, inference masks,
returned IDs or shared evidence.

`capsule` has uniform product-level Size/Color settings, without camera/channel
overrides or a configurable `required` switch:

| Fields | Values / omitted defaults |
| --- | --- |
| `size.enabled`, `color.enabled` | boolean / false |
| `size.target_length` | positive integer pixels / 1 |
| `size.length_tolerance` | nonnegative integer pixels / 0 |
| `color.target_hue` | integer 0..179 / 0 |
| `color.hue_tolerance` | integer 0..90 circular distance / 0 |

Omitted child objects/fields use these defaults; explicit null objects are invalid.
Enabled Size/Color are required checks, subject to the explicit confirmed-EMPTY
skip. Occupancy is always on and has no persisted switch/threshold fields. Recipe
ROI, old presence/blob and old per-check color fields are rejected. There is no
automatic Recipe migration or threshold fitting.

Size compares the longer side of the selected body's minimum-area rotated
rectangle with Target Length, inclusively within Length tolerance. Length spans
pixel centers, not occupied-pixel counts. Both logical views independently require
valid evidence. Body assignment uses the complete Channel ROI, allowing a small
cross-divider tail while conservatively rejecting ambiguous ownership; see the
[inspection contract](architecture.md#inspection-and-results).
Legacy `target_width`, `target_height`, `width_tolerance` and `height_tolerance`
are rejected with explicit guidance, including on disabled Size definitions.
An engineer must explicitly replace these fields with calibrated Length settings;
old W/H targets are never reinterpreted. Loading does not rewrite or delete files.

`image_archive` optionally contains `root_directory` (string, omitted default `""`)
and independent booleans `save_ok`, `save_ng`, `save_error`, `save_empty` (all
default false, including a missing `save_empty` in older archive settings).
Existing Recipes therefore record nothing. Any nonempty root must be a valid
absolute directory path; enabling any switch requires one. Saving validates its
syntax without creating directories or certifying permissions/free space. Runtime
write failures are observable separately from inspection and PLC results.
The responsive Image Archive card edits only this Recipe draft. The four labeled
switches share one row with the directory/actions whenever the card's available
width fits their requirements; otherwise they sit above the directory/actions.
Label/padding clicks toggle the whole condition control. Browse selects a
directory and Open Folder opens an existing directory without creating it.
Save, Discard and lifecycle editing locks use the ordinary Recipe workflow.
Only the immutable successfully started Basler session records images; Folder
Replay, warm-up, tuning and snapshots never record Production archives.
See [archive ownership and output](architecture.md#image-archiving).

## CameraSources

`data/camera_sources.json` stores camera identities directly at the root:

```json
{
  "CAM1": {"folder": "../images/cam1", "basler_serial": "12345678"},
  "CAM2": {"folder": "../images/cam2"}
}
```

Keys are exact identities, not array positions or enumeration/serial order.
Each row optionally contains `folder` and `basler_serial`, defaulting to empty
strings. Optional `mode` defaults to `folder`, `loop` to true and `target_fps` to
8.0; Save omits default-valued fields. Serials must be empty or ASCII digit text.
Invalid present values, duplicate/unknown keys, arrays, embedded `camera_id`,
`source_type`, the obsolete `cameras` envelope and versioned envelopes are rejected.
There is no fallback to `data/sources/camera_sources.json`.

Missing entries, including `{}`, are valid incomplete saved definitions.
Vision presents missing configured cameras with blank fields; only an explicit
edit adds that camera entry. Opening/browsing never fills or rewrites the file.
References outside the current MachineLayout are rejected when applying it.
Basler preparation/connection/Start requires the complete configured camera set
and distinct nonempty serials, resolved in MachineLayout order. Folder definitions
with missing/unavailable inputs may remain READY with a warning; Start preflight
rejects them before engine creation. Saving is not proof of source readiness.
Vision Save can persist an incomplete Basler mapping. Production preparation and
Connect reject it independently; Save never replaces the previous candidate.

Folder paths resolve relative to the new JSON's parent (`data/`); for example,
`../images/cam1` selects entry-relative `images/cam1`. No old base-directory
substitution is attempted. Folder and serial values survive mode changes, even
when inactive. No automatic scanning, identity inference or mapping repair occurs.

`loop` is boolean; FPS is finite/positive. They pace Folder Replay only. Stored
Folder paths are retained across mode changes but are not shown or used as
independent references in Basler mode. They may be empty. Vision Connect all uses
saved mappings. Production Start connects a DISCONNECTED source using the prepared
candidate's validated captured mapping, or reuses a CONNECTED source only when
camera identities, serials and actual dimensions satisfy that candidate. It then
validates ROIs and warms ViMo with synthetic BGR before activation. Busy connection
states, source FAULT and mismatched connections require explicit recovery, not a
second SDK owner or automatic retry.
Switching mode preserves both folders and serials. Disconnect before changing
source mapping/mode; Recipe/Batch/page changes do not disconnect cameras.
Live source keys are `basler:<serial>`; Folder keys are lexical normalized paths.

Loading, browsing and Production Recipe selection do not rewrite source files.
Explicit Vision Save writes the versionless definition atomically. This is source configuration,
not Recipe/ROI data.

## Editing and persistence

Configuration owns the Recipe/Model catalogs and Recipe, MachineLayout and source
drafts. Application coordinates Recipe editing and preparation; Qt presents these
owners without becoming the authority for saved or active configuration.

Recipe stacks Recipe management, Model information, Inspection Rules and one
shared Image Tuning workspace. New/Duplicate, explicit selection, metadata and
model selection retain their drafts. Reload remains a catalog-refresh action,
including external new/repaired files; it is distinct from Discard. Model selection is explicit even with one registered Model; an unset or unknown
reference remains visibly unavailable until selected. The compact Model selector shows its display name and
configured SHA-256 prefix; its tooltip exposes the full ID/hash, not a runtime
verification claim. The second row shows compact default defect-label checkboxes,
wrapping at the available width. There is no filter popup, technical-details or
policy-summary panel. The summary cards use their content's natural height;
one enclosing scroll area updates on wrapping/content/language changes and scrolls
only when needed, leaving Save accessible.
Opening Recipe reads the selected segmentation module's complete
`ModuleInfo.features` catalog through the inference adapter, off Qt dispatch. One
bounded metadata job and one current catalog are owned by the page; stale results
cannot populate another model. Metadata IO does not occupy preparation or gate
Start; Close observes actual job termination before detaching delivery. It
hash-checks a memory snapshot before SDK metadata loading; no inference module,
pipeline or cache is created. Missing/invalid metadata reports an error without
offering an inferred catalog. Guest and Operator see the saved label selections
for the current Recipe. Engineer and Administrator can edit the checkboxes in the
draft and persist them with Recipe Save. Checked labels participate, and unchecked
labels update only draft `defaults.vimo.disabled_labels`. Checking all catalog
labels leaves no filter when no other exclusions exist. Previously saved out-of-catalog exclusions
remain preserved with a tooltip, not offered as catalog choices. Metadata failure
leaves saved/draft exclusions unchanged. Reload or reopening Recipe retries a
failed read. Labels retain exact
case-sensitive identities. Recipe `defaults.vimo` supplies one shared `enabled`,
`required` and `disabled_labels` policy for all configured cameras and channels;
MachineLayout remains the sole authority for their identities and ROI geometry.
Save, Discard and runtime editing locks govern this edit like other Recipe
changes.

The Model selector row also offers **Prepare TensorRT cache**, independent of
Recipe Save. It submits the selected immutable Model identity to the
[application owner](architecture.md#inference); the UI requests initialization only,
so neither connected cameras nor Folder inputs are needed. The adjacent icon shows
unknown, preparing, succeeded or failed with localized accessible names. Preparation can
take minutes; Close waits asynchronously for native work and cleanup, and Start
is unavailable while preparation owns resources. Recipe selection/editing retains
its existing stopped-state rules; changing the Model never retags an earlier result.

Status is a bounded, session-only observation of the latest request, never inferred
from cache files or restored on launch. Success requires initialization and actual
worker cleanup. Preparation performs initialization without sample inference;
it does not certify
permanent cache validity, activate a Recipe or replace Production warm-up. The
operation never saves/discards drafts, changes sources, produces counters/results
or reports to PLC. SDK artifacts remain opaque; no delete/rebuild policy is added.

Occupancy stays always on. The Size/Color rule rows alone open/switch/collapse
tuning; their separate enablement checkboxes never expand it. Both checks may be
enabled, with only one tuning panel visible and no second Collapse action.
Switching panels reuses
the same frozen image and cached measurement ownership. Camera selection, Fit,
Refresh, numeric parameters and image color sampling remain draft-only, with no
production admission, counters, source commands or activation. The former separate
Tune page and duplicate enablement controls are removed. Size and Color summaries
use actual draft limits, including `H <target> ± <tolerance>` for circular Hue.
The compact Size tuning fields use L, pixel units and ± notation; full localized
parameter names remain in tooltips and accessibility labels.

On first selection, Folder tuning reads the camera's first decodable image in the
normal deterministic Folder order. Previous/Next and the image selector browse
that Folder without wrapping; Refresh rereads the selected image, not the next.
Basler tuning instead uses a frozen display frame matching the camera and current
source key, without acquisition or a Folder fallback. Temporary image import is
available in both modes. Each camera retains one selected frozen reference while
tuning remains open; new display publications and switching Size/Color never
replace it. Import/selection/Refresh are explicit changes. Closing tuning releases
its references. IO and measurement share one bounded background job; stale work
cannot attach pixels or measurements to another camera.
The centered image selector reports actual pixel dimensions, `— · W×H` for known
dimensions on a blank canvas, or `ROI ≈` for schematic ROI extents when neither
is available. Short localized image-state descriptions remain accessible;
the tuning canvas retains the detailed standalone-check scope tooltip.
Schematic extents never become measured image bounds or evidence.
Image tools reuse Vision's icons, whole-group wrapping, Fit and 1:1; Recipe
management icons retain localized names/tooltips and the existing confirmations.

Click an upper/lower view in the current image to select its camera, ChannelId
and view. A single detail area beside the parameters shows actual major-axis Length with
configured limits, or actual Hue with target/circular tolerance. View status uses
the existing Size/Color judge's reasons for that capability and view; it is not
the combined channel result or production acceptance. Image overlays keep compact
selected Size or Color check judgments/body outlines and a selection highlight, without twelve repeated
Length captions. Complete major-axis lines and endpoints use original image
coordinates and remain visible across the logical divider; selection retains the
owning view identity. Threshold edits rejudge cached evidence; mode switches retain the
selection. New images/camera changes clear it; stale asynchronous evidence is
never displayed as the selected view's measurements.
Disabled checks, unavailable/pending evidence, execution ERROR and content NG are
distinct; missing scalars display `—`, never ROI-derived measurements. Tune does
not collect Occupancy evidence or infer EMPTY from a missing body. Routine
always-on/unavailable explanations are omitted from Qt presentation; actual
production Occupancy state/evidence and technical errors remain available. This
does not change Size/Color judgment or the production EMPTY skip contract.
Routine tuning scope/success and selection-invitation prose is omitted; the scope
and selection instructions remain in static parameter/canvas tooltips. The canvas
does not aggregate changing per-channel diagnostic text. Selected-view judgment
and localized reasons remain visible without raw measurement-error tooltips. Without a selected
view, a disabled check shows `OFF` rather than unavailable measurements.
Selected-view feedback translates existing missing-body, unusable-color
and tolerance reasons; exact evidence remains in the existing preview results. Missing reference,
pending measurement, technical ERROR and disabled checks remain distinct; a valid
image without usable measurement evidence keeps its existing NG and `—` values.

Recipe Discard beside Save confirms reloading the selected Recipe from its
actual saved file. Read/identity/validation failure preserves the current draft;
MachineLayout and sources are independent. Delete requires a confirmation naming
the saved Recipe and ID, refuses active/candidate references and the normal app's
persisted last-applied ID, and fails closed on unreadable preferences. The catalog
only deletes the unchanged selected Recipe file inside its directory. It never
deletes shared models/images. Success clears selection explicitly without choosing
another Recipe or modifying application references. Runtime editing locks apply
again when confirmed, and persistence errors stay observable.
Vision owns source setup and canvas-first Machine ROI editing, numeric Apply,
Common size and Distribute evenly. Those operations reject invalid bounds atomically;
they never silently clamp.
Vision stacks two subtly contrasted source and ROI containers. One rounded
Folder/Camera track moves its selected thumb without animation; contextual Loop/FPS
or connection/discovery controls align right. Cards retain configured row-major order:
two columns on narrow workspaces, three when at least 1092 logical pixels are
available for cards. Camera headers use the same icon + numeric identity as
Production, with full localized names in accessibility text/tooltips. Card headers
select the displayed camera; inline Folder path/browse and discovered-device
selection edit that camera's existing source draft without selecting it.
Basler serials are selected from discovered devices, not entered manually.
Undiscovered saved assignments remain visible and unchanged; discovery never
auto-assigns or clears them. Devices used by another card cannot be assigned twice;
clear the previous assignment explicitly before moving one. Device model/serial,
connection state and actual dimensions are shown only where observed.
Connected serial/model/dimensions come from the application-owned connection
snapshot and survive leaving/reopening Vision. An empty page-local discovery
list means no discovery evidence, never disconnection; only discovered devices
are newly assignable. Actual Disconnect/source FAULT still determines status.
Discovery reads device information off Qt dispatch without opening cameras or
assigning them by enumeration order. Cards show explicit connection state; assignment alone is not
connection status or live preview. Connect all/Disconnect are stopped operations
off Qt dispatch, owned by the application rather than the widget. Source fields
are locked until Disconnect; ROI editing remains subject to inspection locks.
There is no separate source dialog. Folder Loop and an editable integer FPS stepper
are directly accessible on the source toolbar. Explicit FPS edits accept 1–10;
boundary step buttons are disabled. Existing fractional or out-of-range FPS values
remain unchanged, with the full value in a tooltip and stepping disabled until
explicit valid entry. Focus changes never clamp or rewrite them. This UI range does not
change the persisted positive/finite FPS contract. Basler does not use these pacing
settings. Connection/discovery actions are compact icons with localized tooltips;
the connection summary is numeric, with a localized accessible state and no
runtime tooltip.
Source cards do not duplicate device/status details in tooltips; full Folder paths
and selected device identities remain on their fields. Routine prose is omitted;
Vision errors use the existing bottom feedback strip. Specific setup/ROI/source
recovery details remain available there, without duplicating normal runtime state.
That feedback is page-scoped and never supersedes application FAULT or shutdown.
The global strip normally shows the saved source mode rather than repeating READY:
Folder is a source selection, not device connectivity or a continuously verified
camera count. Camera counts come from actual connected CameraId/serial matches.
Unsaved selections do not change that strip. Lifecycle transitions and errors keep
precedence; source availability does not establish full inspection readiness.
One visible Save action sits beside Discard; a compact status symbol exposes
clean, dirty, saving or failed state through its symbol and localized accessible
name, without a runtime tooltip.
The image selector and previous/next browse Folder inputs. Import loads a temporary
image; Use runtime image freezes the latest eligible same-camera/source display
frame, with explicit refresh. Neither tracks live pixels. Fit and 1:1 retain their
canvas transforms. The image toolbar center always describes the current canvas:
real pixels show dimensions and the Folder index where applicable; a blank view
shows known dimensions as `— · W×H`; an unbounded view with ROIs says `ROI ≈`, with
its illustrative meaning in a tooltip. Without dimensions or initialized ROIs it
shows `—`, with ROI editing unavailable until dimensions are known. The chooser
retains image identity/blank-state meaning; the canvas does not duplicate it.
Capture keeps a static action tooltip; waiting/cancellation use the existing
control states, successful pixels use the image selector, and failures/timeouts
use the global feedback strip. The chooser list is separate from this caption;
source changes and asynchronous image delivery cannot reuse an old schematic caption.
The canvas does not repeat status text. ROI channel selection changes
the displayed slot; numeric Apply edits only the machine draft until Save.
Mode selection edits the same CameraSources draft and requires explicit Save;
selecting a card never changes saved sources or geometry. Runtime editing locks remain.
Save permits structurally valid ROIs without canvas images; numeric
editing does not require image pixels. Available connected dimensions supply bounds
checks, always repeated before Start. Without pixels, known dimensions display a
blank bounded canvas with the saved/draft ROIs. Without trustworthy dimensions,
ROI extents provide a dashed, explicitly labeled schematic only: they never become
camera dimensions, draft bounds, Frames or persisted data. Selection/Fit/zoom and
numeric editing remain available; real bounds reject incompatible geometry without
repair when an image or connected dimensions become available.
Independent Save does not require Folder availability, a Recipe/Model or an engine.
It saves CameraSources even when other setup is incomplete. MachineLayout is saved
only when every selected camera has complete, valid geometry; partial ROI edits
remain an unsaved draft. Each file has its own outcome: a source Save can succeed
while MachineLayout validation/write fails, or vice versa. Invalid or externally
changed saved files require explicit reload/repair and are not silently replaced.
Save never prepares/applies a candidate or changes the connected source.

After valid MachineLayout/source Saves, Recipe editing, explicit catalog Refresh
and Production selection are available in the same window/process. No Application,
bridge or frame store is replaced. The selected Recipe must still resolve its Model and
pass artifact/source validation. Only successful preparation installs a candidate;
a successful Start activates it. Saved-but-incomplete setup remains UNCONFIGURED. Folder input
availability is checked at Start, before engine creation. Drawing needs an
actual image; synthetic warm-up images are never published. Images are temporary
canvas content, not independent persisted
reference configuration. Replacing an image never rescales, repairs or saves ROIs.

With Basler connected and inspection stopped, **Capture image** waits for the
selected camera's next valid external-triggered frame (ten-second deadline).
It never sends a trigger. Only one request can be pending; Cancel, camera/source
changes, Disconnect, Start and Close invalidate it. Waiting, timeout, cancellation
and success are explicit. Delivery is asynchronous and owns immutable BGR pixels;
late callbacks cannot replace a different camera's image. This operation does not
publish Production frames, infer, count or report. During inspection only existing
frozen Runtime viewing is available. No image history/export/library is created.

Tune preview measures the current draft without Production admission, accounting,
candidate activation or source reconfiguration. Frozen-reference ownership,
camera/mode switching and cache invalidation remain separate from live display.
No matching pixels means no measurement/judgment overlay. Black pixels in a valid
frame are still image evidence: enabled Size/Color can report missing-body/unusable
color NG with unavailable numeric measurements. Tune has no Occupancy fusion and
does not reinterpret that result as Production EMPTY or consume historical NG.

Recipe **Save** persists only its definition, including incomplete Model references.
It does not require saved/complete MachineLayout or source drafts, prepare a
candidate, change `last_applied_recipe_id`, or alter an already prepared candidate.
Dirty Recipe navigation/close offers Save, Discard or Cancel with these same semantics.

Production's compact saved-Recipe selector prepares the explicitly selected ID
using its current saved Recipe, MachineLayout, Model and source definitions, off Qt
dispatch. Its catalog refreshes on page entry and through Refresh. Refresh lists
incomplete Recipes and reports invalid/ambiguous files without selecting anything
or replacing the candidate. The selector reflects the installed candidate without
preparation, including startup restoration and a candidate whose saved file is no
longer available. Such an unavailable catalog entry cannot be newly selected.
Refreshing a renamed saved definition does not relabel the prepared candidate.
The state and optional active/requested identity share the first header row;
the selector and Refresh occupy the second. Pending preparation identifies the
request separately while the selector retains the installed candidate. Failure
keeps that candidate selected and names the failed request in feedback details.
The selector's full tooltip/accessibility retains preparation-failure details even
when an earlier Start warning has priority in the bottom strip.
Recipe editor selection and unsaved drafts remain independent. Reselecting the same ID prepares the latest saved version. While
preparation is pending, conflicting selections and Start are unavailable. Closing,
a changed Production generation/candidate or a failed preparation prevents late
installation; the previous candidate and active configuration remain intact.

Successful candidate installation updates `app.json`'s `last_applied_recipe_id`;
preference write failures remain visible and do not undo candidate installation.
Startup can restore the latest saved version of that ID as a stopped candidate,
with no automatic fallback or Start. Candidate, active and persisted applied IDs
remain protected from Recipe deletion.

Vision is save-only. MachineLayout and CameraSources retain independent validation
and atomic persistence; a partial save is not a cross-file rollback. Save never
prepares/installs a candidate, changes its Recipe identity or `last_applied_recipe_id`,
or activates a session. Model binaries and source images remain unchanged.
Runtime lifecycle and source-connection locks continue to govern editing.

A prepared candidate retains its saved MachineLayout and CameraSources definitions
and their paths. Successfully saved or reloaded definitions are compared by parsed
value, so unchanged saves do not invalidate it. A changed dependency requires
explicit re-preparation while retaining the candidate, active identity and saved
Recipe preference. Partial saves observe only the definitions actually persisted.
Production disables Start when this state is known and shows re-selection guidance.
Reselecting the same saved Recipe prepares current dependencies; failed preparation
retains the old candidate and does not clear its re-preparation requirement.

Every Start also parses and compares those files on the Application lifecycle worker
before automatic camera connection, session construction or engine creation.
External changes, missing or unreadable
dependencies reject Start without activating the candidate. Re-preparation clears
this requirement only on successful installation. Start uses the captured sources
with the captured layout; it never combines newly read sources with an older layout.
Already active sessions keep their immutable definitions. Explicit fixed-image
launchers retain their own image/source ownership and do not adopt CameraSources.
Their dependency snapshots capture the launcher-owned mapping without a source
file path. Whenever a snapshot is present, missing captured sources reject Start;
the factory never substitutes a later disk definition. Launch paths without a
dependency snapshot retain their established source selection.

Vision's restore icon beside Save confirms discarding all unsaved source,
Loop/FPS and machine geometry edits together. It reloads and validates the current
files off Qt dispatch before replacing either draft/baseline, including partially
saved multi-file outcomes. Read/validation failure keeps both drafts and reports
details in the bottom strip. It neither writes files nor prepares/activates a
candidate, commands sources or resets counters. Viewing pixels, selection and zoom
remain temporary viewing state. The action is disabled when clean, editing is locked,
IO is busy or a capture is pending. Changed saved camera identities/order require
reopening the application; connected sources must be disconnected explicitly before
reloading an externally changed source mapping. Navigation's Discard uses the same
file reload and waits for its completion.

Files use same-directory temporary write, fsync and atomic replace. Failures retain
the previous file where replacement did not succeed, and preserve distinct partial
success feedback across multi-file operations. Do not hand-edit running sessions
or treat a saved candidate as the active session's configuration.
