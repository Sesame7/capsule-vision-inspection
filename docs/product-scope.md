# Capsule Vision Inspection — PRODUCT-0 Product Scope

> **Status:** Reviewed PRODUCT-0 target draft · 2026-11-04
>
> **Purpose:** Define target behavior and acceptance requirements, not current capability. Explicitly open decisions remain unresolved.

## Document authority and terms

This document owns the target product decisions. It does not authorize their
implementation or supersede validated behavior outside an approved Phase.
[AGENTS.md](../AGENTS.md#documentation-authority) defines contributor obligations;
the explicitly authorized Phase defines the permitted behavior change, Preserve
requirements and validation. Update current contracts only as those changes are
implemented and validated. Report material conflicts rather than treating a
target statement as proof that a capability exists.

| Current contract | Responsibility retained during product changes |
| --- | --- |
| [Architecture](architecture.md) | Domain boundaries, activation, accounting, worker/resource ownership, shutdown and presentation identity |
| [Configuration](configuration.md) | Definitions, persistence, account/role policy, saved versus active state and asset protection |
| [Communication](communication.md) | Protocols, reporting attribution, register ownership, uncertainty and diagnostic admission |
| [Field UI validation](field-ui-validation.md) | Native evidence, normal launch/Close, isolated fixtures and field safeguards |

These contracts remain authoritative for current operation. A simpler target UI
does not remove their safety, lifecycle, persistence or communication guarantees.
The requirements below describe differences as well as retained capabilities;
they are not a release checklist claiming completion.

| Term | Meaning in this target |
| --- | --- |
| Production Batch | Operator-defined production scope with a durable identity separate from its displayed Batch ID text; may span Runs and application restarts |
| Run | One successfully started inspection session; a new successful Start creates a new Run |
| Cycle | One identified inspection occurrence with its configured camera results; the History detail unit |
| PLC group | An attributable physical group of four Cycles, established by the reporting owner under the equipment phase/counter contract; not the operator's production Batch |
| PLC counter | W48/W148 value used in attribution; wraps/repeats and is not a globally unique group identity |
| Final group outcome | One OK/NG/EMPTY product outcome per channel in a completed attributable PLC group; distinct from per-Cycle business/technical evidence |
| PLC reporting / production export | Respectively, protocol result delivery to the PLC / a manually requested Excel summary |

Inspection retains ownership of per-Cycle evidence and judgments. Reporting owns
PLC-group membership, progress, completion timing and the authoritative reject
bitmap. Application owns activation, production Batch/Run identity, accounting and
persistence coordination. Qt/Web present these facts without another group
assembler. This assigns responsibilities within the existing layers; it does not
prescribe a new service, database or framework.

## 1. Product boundary

- Industrial multichannel capsule vision inspection and local equipment-management application; prioritize reliable on-site production, useful engineering configuration, and simple operator workflow over speculative features.
- One IPC supports **30 or 36 channels**. A 72-channel machine uses **two independent 36-channel IPCs**; the vision software does not synchronize them across IPCs.
- Basler is the production acquisition source. Folder Replay remains available for demonstration and engineering/testing.
- Continue supporting the existing dark/light themes, supported **64 UI languages**, and the optional read-only Web Observer.
- The standard application retains its **five primary pages**: Production, Recipe, Vision, Communication, History. New records appear inside History, not as additional primary navigation pages.
- Existing mature engineering functions should be preserved when useful. Simplification means reducing unnecessary behavior/complexity, not arbitrarily shrinking cards or removing buttons.

## 2. Roles and operating workflow

- Default entry is **Guest** without a login wall. Guest can view allowed information and use safe local Folder Replay demonstration without PLC reporting. Retain Operator, Engineer, and Administrator responsibilities according to existing authorization contracts.
- Switching user only replaces the active session **after the new login succeeds**. Canceling or failing a switch by itself preserves the otherwise valid current login; it must not undo a later logout, account change or Close. Preserve stale-completion protection and current-account disable/reset behavior.
- The Operator selects a saved Recipe, optionally sets a Production Batch, and clicks **Start**. Enabled four-cycle Basler reporting can start while triggers are arriving. It waits for an observed PLC counter transition and a clean source boundary before reporting a complete group. Production passively receives hardware triggers; it never commands the machine to mechanically start, stop, or pause.
- For Basler, **Start automatically connects required cameras if needed** and performs Start preparation/warm-up. Reuse an already valid connection. This does not merge Recipe Save, candidate preparation and activation. Vision retains manual Connect all / Disconnect for engineering use under the existing admission rules. Stop stops inspection but does **not** disconnect cameras. Connection/start failures remain visible and do not fabricate production results.
- Basler Production exposes **Start / Stop** (and fault Reset when needed), not Pause / Resume / Step. Folder Replay retains appropriate playback controls.
- Each IPC identifies a Production Batch by its trimmed, nonempty, case-sensitive label and retains a stable internal UUID. Selecting a new label starts its own totals. Selecting a previously used label continues its original Batch and totals, including after an explicit End.
- Switching labels changes the selection. End Batch records an end event and clears the selection after confirmation. Startup restores the last selection and totals and waits for an explicit Start. Each successful Start creates a distinct Run, including Runs that stop before the first Cycle. A named Batch requires durable Run admission.
- Basler can Start without a Batch in every reporting mode. Retain the unbatched Run, its Cycles and eligible PLC groups when storage supports them. If recording is unavailable or incomplete, show that uncertainty and allow inspection. Later Batch selections apply only to subsequent Runs.
- Switching Recipe does not itself reset the operator Batch's cumulative quantities. Exact constraints on changing Recipe while running remain subject to the current lifecycle contract unless deliberately changed in a later Phase.

## 3. PLC reporting, result semantics and display

### 3.1 Inspection flow and accepted baseline

One PLC physical group contains **four exposure Cycles**. Each channel is
expected to retain the **same physical capsule rotating in place** across those
exposures. Each camera image includes **six channels**; the upper and lower
regions of each channel ROI are **two optical views obtained through a mirror
arrangement**. They are not separate capsules or necessarily literal capsule halves.
Stable channel-to-capsule identity is an equipment assumption, not software proof
of physical tracking. Rare mechanical deviations do not authorize new detection
or recovery logic here.

The target inspection policy retains the current algorithm as an accepted product
baseline, not a claim of globally optimal accuracy or throughput:

- **ViMo inspects the full camera image on every exposure.** Retain existing
  defect-label filtering and channel-ROI assignment. Channel-associated defect
  evidence that survives the label filter requests rejection.
- Background-only findings require no new rejection policy or precise capsule-mask
  localization in this scope. Preserve existing handling of unassigned camera
  findings; lack of ROI assignment alone is not proof of harmless background.
- **Occupancy** establishes presence/absence evidence and supports conditional
  traditional checks. Enabled **Length** (the existing Size check) and **Color**
  evaluate both optical views on each exposure using the existing implementation.
  Confirmed EMPTY may skip Length/Color, but **does not suppress ViMo** or erase
  defect evidence.
- Retain per-exposure traditional checks. Once-per-four-Cycle sampling requires
  measured performance and detection evidence before a separately approved change;
  it is not an authorized optimization.

The [inspection contract](architecture.md#inspection-and-results) owns current
evidence, measurement and judgment details; [Recipe settings](configuration.md#recipe)
own enablement, label filters and thresholds. No ROI, threshold, Recipe format or
execution schedule changes are implied. The current configurable ViMo enablement
requires the [decision below](#10-open-decisions-and-audit-boundaries) before the
every-exposure target can be enforced.

### 3.2 Four-Cycle decision and source of truth

- The product supports **per-Cycle reporting** and the formal **four-Cycle PLC-batch reporting** option. The former remains for supported alternative configurations; the latter is the normal intended hardware workflow.
- The W48/W148 PLC counter identifies a physical group of four triggers **only when the reporting owner has established valid batch attribution under the agreed hardware phase/counter conditions**. A mere sequence of four observed CycleIds is not, by itself, proof of physical batch identity.
- Use the **authoritative per-Cycle reject bitmaps and their bitwise OR**, not a second projection from displayed statuses. Verified EMPTY means the evidence-qualified non-reject case in the [PLC projection contract](communication.md#first-station-registers); a business EMPTY label alone is insufficient.

A completed attributable group contributes **one final business outcome per
channel**, distinct from internal per-Cycle judgments and technical assessments:

| Final OR reject bit | Per-Cycle evidence | Final group outcome |
| --- | --- | --- |
| Set | Any definite defect or conservative reject request | **NG**, with a reject request |
| Clear | All four observations are verified EMPTY | **EMPTY**, without a reject request |
| Clear | All observations are OK or verified EMPTY, with at least one OK | **OK**, without a reject request |

Preserve intermediate **UNKNOWN**, technical **ERROR**, disabled/neutral checks
and original reasons. Final NG includes conservative rejection; it does not claim
that every rejected capsule has a confirmed defect. A partial/error Cycle with
an attributable terminal bitmap is not a missing Cycle and retains its existing
conservative contribution. Fewer than four attributable terminal results or invalid
group attribution must not become a fabricated completed group or guessed PLC
report. Inspection completion, report delivery and mechanical action remain
distinct: a result-reporting **ACK does not prove mechanical ejection**.

The [four-cycle reporting contract](communication.md#four-cycle-reporting) owns
implemented attribution, reject projection and completion timing; the
[architecture contract](architecture.md#display-and-web) owns bounded presentation.

### 3.3 Production presentation

- In four-Cycle mode, main counters are **Batches, OK, NG, EMPTY, Batch Duration**. Here Batches counts completed attributable PLC groups, not operator production Batches; count each channel once per group. Batch Duration runs from first Cycle admission to the fourth terminal result, using the reporting owner's monotonic clock. Publish it only for eligible groups; exclude subsequent attribution checks, writes and ACK. Cycle diagnostics remain separate.
- Keep 5/6 responsive camera cards and their six channels per camera. In four-Cycle mode each channel is represented by a stable **2 × 2 mini-tile**. Populate one cell per completed attributable Cycle: first top-left, second top-right, third bottom-left, fourth bottom-right. Green OK, red reject/NG, grey confirmed EMPTY, neutral for not-yet-observed. A new group's first completed Cycle resets the tile sequence.
- In four-Cycle mode, update each camera's displayed image **once per completed group**, using the corresponding **fourth** frame. Overlays apply only to that displayed frame, never aggregate bounding boxes from earlier frames. The image may therefore remain from the preceding group while the new group's mini-tile advances; the UI must not misrepresent its identity.
- A new eligible group clears any camera lacking its valid matching fourth frame to the startup empty-image state; other cameras display normally. Never substitute another exposure. Incomplete/invalidated groups do not count; phase loss neutralizes unconfirmed progress without retracting prior eligible completions or their retained images.
- Maintain bounded presentation work; do not add an image cache of all four frames just to animate the UI. Per-Cycle and Folder presentation retain suitable current per-Cycle behavior.
- Preserve **per-exposure** image saving with independent Recipe OK/NG/ERROR/EMPTY selections, with or without a Batch. After NG and technical ERROR take precedence, a complete camera image with at least one OK capsule and only OK or confirmed EMPTY channels qualifies for OK archiving. Six confirmed EMPTY channels qualify for EMPTY archiving. See [image archiving](architecture.md#image-archiving) for evidence and file codes.

## 4. Recipe and Vision

Preserve the [Recipe](configuration.md#recipe),
[MachineLayout](configuration.md#machinelayout),
[CameraSources](configuration.md#camerasources) and
[editing/persistence](configuration.md#editing-and-persistence) contracts except
for an explicitly approved change. The product requirements are:

- Retain the mature Recipe editor rather than a broad redesign: new/copy/delete/save/discard, product code, model selection/management, ViMo label selection, mandatory Occupancy logic, optional Size/Length and Color controls, image tuning, and image archive settings.
- Keep the existing model-management actions visible; the card has adequate room. **Prepare TensorRT cache** remains an explicit useful engineering action.
- Saving a Recipe definition is separate from selecting/preparing/starting it in Production.
- MachineLayout, camera source assignment and ROI belong to **equipment-level Vision configuration**, not ordinary product Recipe switching. Retain the existing 30/36 channel source, capture, preview, ROI editing, and save workflows.
- A possible future small set of PLC *product process parameters* in Recipe is **deferred** pending the actual signal/address/activation contract. PLC transport/IP/base addresses remain equipment-level Communication settings; do not build a general PLC parameter framework now.

## 5. Communication and Web

- Communication is primarily an Engineer workspace: persist settings once, and have normal Production Start use the saved settings. Operator should not need to visit Communication each shift.
- Retain **FINS/TCP**, **FINS/UDP**, and **Modbus/TCP**, with their existing applicable endpoint and validation rules. Preserve per-Cycle reporting.
- Offer only `per_cycle` and `four_cycle` reporting, with a separate Enabled setting. New normal installations select four-cycle/FINS-TCP/W49 with reporting disabled. Obsolete `w48_observe` and `w48_active` settings are invalid, without migration or automatic replacement. Retire Observe-only execution and Pilot Evidence customer UI; preserve bounded engineering diagnostics and attribution evidence.
- For the two IPCs, the configured four-Cycle FINS/TCP reporting commit base must support **W49 or W149**. Corresponding counter reads are **W48 or W148**. For 30 channels write two bitmap words; for 36 write three. The [current communication contract](communication.md#four-cycle-reporting) owns the implemented register, attribution and protection rules; equipment acceptance remains separate.
- Engineer can save **reporting disabled** and still run pure inspection and statistics. Production clearly states **PLC reporting disabled**, without implying successful delivery. This does not itself disable independent alarm monitoring. Disabled reporting performs no automatic PLC reads/writes or group attribution; retain per-Cycle occurrence counting and Guest replay without inventing groups. Saved reporting edits activate at the next Start; do not unexpectedly mutate an active reporting worker.
- Retain independent PLC Diagnostics endpoint, manual read/write, grouped watches, automatic refresh, DEC/HEX presentation, and ownership/authorization/write-protection behavior. Only targeted layout simplification is planned (including a compact, readable manual-write row).
- Web Observer remains an **optional, read-only service for a trusted LAN**, without an independent Web login system or remote Production control. New four-Cycle presentation should reuse authoritative batch state, not invent Web-specific grouping.

## 6. History, images and manual production export

### 6.1 Inspection History

- **One History record per Cycle** remains the storage and detail unit. Keep the existing camera/channel detail matrix, per-Cycle reasons, time filtering and result lookup.
- Associate the four observed Run/Cycle identities with their eligible physical group regardless of History arrival order. Retain group timing and final outcomes for the same 30-day detail window.
- Persist enough association to distinguish the production Batch, Run, PLC group and Cycle identities defined above; PLC counter text alone is not a group key.
- Do not convert a lack of verified physical association into a fictitious group. Where a reporting mode does not establish that association, keep Cycle records without an invented physical-group identity.
- Existing inspection History records/old database format need **no migration compatibility**. The owner will manually clear affected old History data during an approved upgrade. This decision does not cover account storage or authorize cleanup now. The application must **not** silently delete the user's database or other assets during normal startup.
- **Keep the current 30-day History retention**; no new retention controls in PRODUCT-0.

### 6.2 Archived image lookup

- Archive images independently of History. Time, camera, Cycle, and—if needed—an adjusted image naming convention may be used to support manual identification.
- A simple **Open photo / Open containing folder** action may make a best-effort match. No image index, mapping database, guaranteed match, or image-management system is required. Ambiguous matches must not silently open an unrelated photo; missing/deleted files can simply be reported.

### 6.3 Manual production export

- **Manual export only**. Neither Stop nor production Batch change automatically writes a report.
- Target export is an ordinary **Excel (.xlsx)** file with a concise production summary and PLC-group results sheet; final presentation and format detail may be refined in the scoped export Phase. Do not embed all photos or duplicate the complete per-Cycle History as an extra sheet.
- Summarize a **single explicitly identified production Batch** across all its Runs, including Runs after application restart. Include Run start/stop observations, Recipe identity where relevant, and one final OK/NG/EMPTY result per channel per attributable completed PLC group.
- Retain Batch and Run identities and cumulative occurrence and physical-group totals across restarts and detail expiry. Export must distinguish uncertain totals (including an interrupted Run or lost facts) from expired detail. Best-effort Cycle rows alone cannot establish completeness.

## 7. Audit Trail and alarms

- History page becomes three clear tabs/branches: **Inspection History**, **Audit Trail**, and **Alarm History**; keep the five existing primary navigation destinations.
- Audit Trail records meaningful human/security events: important account/session actions, Production Start/Stop/new/continued Batch/Recipe selection, Recipe/Model/Vision/Communication changes, manual PLC writes, and necessary failures/authorization rejections. Do **not** record navigation clicks, routine refreshes, every photo, or other trivial interactions.
- Each audit entry should have time, actor, action, target, result, and a short informative description. For material configuration changes, use meaningful before/after values when affordable; never record passwords or sensitive secrets. Audit is locally persistent, with no dedicated export feature initially.
- Alarm monitoring is **independent of PLC detection-result reporting**. When configured and enabled, it can run after software startup while Production is READY, RUNNING, or stopped; unavailable communication is an **unknown/unavailable monitor state**, not proof that all PLC alarms cleared.
- PLC retains responsibility for equipment/mechanical safety response. Software displays actual PLC alarm signals and important software faults, recording alarm occurrence and recovery transitions rather than every poll. Preserve current application FAULT/cleanup semantics for serious software failures. Do not add alarm acknowledgment, complex severity editors, sound systems, or remote notifications in the first implementation.
- PLC alarm observation/reads must not disturb the timing or ownership of critical four-Cycle reporting reads and writes. The concrete transport/scheduling integration needs its own design and validation.
- Retention, access policy and equipment signal mapping remain [open below](#10-open-decisions-and-audit-boundaries). Independent alarm monitoring must have an explicit authorization/ownership contract; existing session-bound diagnostic watches are not implicit permission for startup alarm reads.

## 8. Global UI and multilingual quality contract

### 8.1 Product UX

- Retain the current PySide6 shell, shared theme tokens, icon style, page structure, and meaningful engineering actions. No speculative new UI framework or general component-system redesign.
- Normal operation should be compact in *steps*, not necessarily compact in pixels. There is ample vertical space; do not collapse mature Recipe/engineering cards merely to reduce visible controls.
- Reduce redundant text in routine views. Keep failures, configuration/operational distinctions, actionable next steps, and important states plainly visible.

### 8.2 Table readability and allocation

- Tables must **not leave one important column visibly cramped** while adjacent columns have surplus space. Before distributing excess width, measure and reserve a sensible readable minimum for **every** column based on the localized header, representative/common values, font metrics, cell padding, and icons/status indicators.
- Distribute remaining space according to content need, instead of rigid equal fractions or a globally fixed narrow width for a data-heavy column. Do not let an excessively long optional value steal space required for a short but essential column.
- Preserve the ability to read core values **without relying on tooltips** when the window has sufficient room. Long, genuinely unbounded dynamic text may elide with full tooltip/accessibility text.
- If the viewport genuinely cannot fit sensible column minima, prefer a deliberate responsive fallback (layout reflow or table horizontal scrolling where appropriate) over squeezing columns to unreadable widths. Do not increase the application's minimum supported screen requirement casually.
- Apply this rule especially to **History** (all three branches), **PLC Diagnostics Watch**, **Recipe/model tables**, and any future Alarm/Audit tables.

### 8.3 Language-change stability

- Keep the existing 64 language IDs, native-language selector, Chinese/English base catalog, and optional English fallback for keys whose current contract permits it. Do not weaken existing mandatory native coverage, including identity UI. Actions, captions, errors and accessible labels added by new features must use the existing localization system. Technical IDs/codes and user-defined names remain unchanged.
- At a given supported window size, **when spare room exists**, use shaped text measurements/budgets across supported localized captions and relevant button states so language switching does **not unnecessarily resize controls, shift neighboring actions, or make rows jump**. Do not determine visual width by character count or reserve English-only sizes.
- Stable widths are a preference conditioned on available space, **not** an absolute requirement to reserve the single longest translation at the expense of important content. In genuinely constrained layouts, reflow deliberately and keep required controls available.
- Buttons with state-dependent labels should reserve the required action budget so ordinary state changes do not move surrounding controls. Avoid unnecessary geometry changes on mere status/timer updates.
- Preserve existing light/dark themes, readable glyph fallback, tooltips and accessibility; do not change the language catalog or font distribution as a side effect of layout work.

### 8.4 Visual validation expectations

- Validate relevant changed pages with **native Qt measurements and real screenshots**, not only static source inspection. Use the existing supported **1080-logical-pixel** layout target and a wider **1920-logical-pixel** case where appropriate; retain the repository's stated field UI validation safeguards.
- Include simplified Chinese, English and selected longer localized captions/scripts as representative cases, plus available-width checks. Prefer automated width-budget tests over manually inspecting all 64 languages; do not claim coverage that was not run.
- Review whether a column is disproportionately compressed, whether relevant headers/values are legible, and whether switching languages/state visibly moves stable action groups despite available room. Do not declare success merely because no control overlaps or because full text appears in tooltips.
- A Windows field workstation can provide human screenshot/interaction confirmation when needed. **Isolated UI previews must not connect cameras, send PLC commands, change live configuration or touch customer data**; follow [field UI validation](field-ui-validation.md).

## 9. Explicit Preserve / Out of scope

### Preserve

- Contract-first layering and current lifecycle/resource ownership; safety of actual PLC writes and attribution; existing data-loss/uncertainty signaling; reliable shutdown; hardware-bound frame and result identity.
- Existing validated per-Cycle transport, live 4-Cycle production semantics pending deliberate changes, Recipe/model/ROI behavior, Folder demo, necessary engineer controls, UI language and theme support.
- Protection of operational accounts, model binaries, configuration, image archives, and local data. Lack of legacy History migration does **not** authorize indiscriminate asset cleanup.

### Product exclusions

- A new architectural framework; cross-IPC reporting synchronization; machine mechanical controls; arbitrary per-Recipe PLC address editors; guaranteed image indexing; broad History migration; advanced Web authentication/control; alarm acknowledgment/notification systems; speculative performance optimizations.

## 10. Open decisions and audit boundaries

The following details are unresolved; they do not reopen the target decisions
above or authorize choosing a convenient implementation silently.

- **ViMo enable policy:** the current Recipe permits ViMo to be disabled. Decide
  how existing disabled Recipes will be handled before enforcing the target
  every-exposure inspection policy; this document does not reinterpret their
  saved settings or authorize an implicit enablement change.
- **Export completeness:** determine how the workbook presents interrupted Runs,
  uncertain totals and expired detail for the selected Batch.
- **Audit and alarms:** decide retention, querying and role access; 30 days is a
  proposal for these records, not an approved requirement. Define authorization
  for saved startup monitoring while Guest or after logout, independently of
  session-bound manual watches. Verify alarm addresses/bit semantics against the
  approved equipment signal table before implementation.
- **Feature acceptance details:** settle Excel layout, deterministic best-effort
  photo matching, and representative readable minima/geometry criteria for each
  changed table in their respective scoped Phases. The `.xlsx` format, manual-only
  export, no image index and content-aware table requirements are already decided.

The next code-level audit should trace existing owners and alternate entry paths
for session switching, Start/connection/Close, reporting attribution and counters,
History association/write timing, Batch/Run durability, Qt/Web frame identity and
diagnostic monitoring. Compare changed-table requirements against actual native
geometry before proposing layout work. Preserve existing archive, authorization,
loss/uncertainty and cleanup boundaries throughout that audit.

Only after that audit should a Keep / Modify / Remove / Add assessment identify
files, conflicts and independently approvable implementation Phases. This document
does not establish their order, storage design or final roadmap. Use the existing
contracts for implemented details; propose a separate plan only if the audit
demonstrates an ownership or duplication problem it would solve. Later English
implementation prompts should authorize one Phase at a time with explicit behavior,
boundaries, Preserve requirements and validation.
