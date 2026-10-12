# Production UI exploration

**Historical — selected design rationale, not Current UI requirements.** The
Production Screen v0.1 exploration closed on **2026-10-01**. This archive was
consolidated on 2026-10-12 from `docs/production-ui-direction.md` and the retained
local prototype record.

The original direction was committed as
`1d2ed8cec82fb141b97831da856d090b75f656f3`. The source read for this migration
was the mixed document at checkpoint
`8fad326aaaf5e0e981224963607a7e000eb95604`, SHA-256
`764e046fe245b09429f44c3c8ccd3d185bf6cd147a5fdbeddecf5dab5f0e34b7`.
It already contained later Production and global-shell annotations. This archive
is **not an untouched October 1 snapshot** and does not adopt its old proposals
as newly approved Target requirements.

Current presentation belongs to [UI shell](../ui-shell.md); Recipe/Vision editing
belongs to [UI configuration](../ui-configuration.md). Technical/product meaning
belongs to [inspection](../inspection.md), and accounting/activation belongs to
[application](../application.md).

## Workstation context and visual rationale

The exploration assumed a vertically mounted industrial PC with an approximately
**1080 × 1920 portrait display**, used by operators, engineers and commissioning
technicians through touch, mouse and keyboard. It aimed to show six camera views
simultaneously rather than let an occasional enlarged view dictate the whole page.

The chosen direction was called **iOS-inspired Industrial Dark**: dark neutral
surfaces, restrained normal-state color, clear hierarchy, concise bilingual labels,
icon plus short-text navigation and touch-aware controls. The name described
design principles, not a phone/tablet interface.

The exploration deliberately avoided large decorative cards, glass effects,
decorative animation, mobile tab bars, gesture-only actions and bright saturated
normal states. Images, defects and technical abnormalities were intended to draw
attention before decoration. Exact palette/type/spacing/radius values remained
examples for later implementation and physical review, not a CSS-to-QSS contract.

A compact navigation and summary above a **2-column × 3-row portrait workspace**
kept the six images dominant. The proposed camera header combined camera identity,
six channel indicators and secondary processing time. Permanent metadata/footer
rows and large textual result panels were discouraged to preserve image space.
A large fixed sidebar was not favored on the portrait workstation.

The prototype used Chinese/English text and real black-background orange/golden
capsule photographs to expose contrast/spacing questions. Source-image content
and aspect ratio mattered more than filling every card. The direction source
mentioned discretionary cropping; that latitude was exploratory, not authority
to alter Current source-coordinate rendering.

## Result meaning and restrained emphasis

The exploration distinguished successful product OK, product NG, pending/disabled
or unavailable results, and known technical invalidity. Muted OK, local red NG
and amber technical warnings were intended to keep normal production quiet.
Shapes/icons and concise text were needed alongside color; essential meanings
could not depend on hover alone.

A known failure was not an absent result and could not disappear into neutral
pending styling. Camera uncertainty or inference failure was not evidence that
six capsules were defective. A local product defect was not a reason to turn
the entire camera card red. A global technical fault needed a distinct cue;
the prototype's violet example was not a fixed future color requirement.
Product NG and technical error could coexist, as later represented by ERROR + NG.

Camera/slot position supplied the primary channel association; exception IDs and
interaction could expose numbers without 36 prominent labels dominating the page.
The illustrative mapping was CAM1 channels 1–6 through CAM6 channels 31–36.
Neither HTML fixture positions nor fixed counts replaced configured identity/layout
contracts. Physical mapping speed and touch disclosure still needed human testing.

Software Start/Stop simulated inspection flow. The design explicitly did not make
“Stop Inspection” a machine emergency-stop action. Machine hardware safety was
outside the prototype and remains unvalidated by these visual records.

## Exploration versus later implementation

The mixed source contained both early proposals and later implementation notes.
The following classification was made at consolidation; detailed Current behavior
is deliberately linked to its owner rather than copied into this frozen archive.

| Historical idea | Later status / evidentiary limit |
| --- | --- |
| Dark, restrained, bilingual shell with six visible camera cards | General direction was implemented in PySide; exact layout, icons, sizes and responsive behavior are owned by UI shell, not prototype geometry. Physical usability was not established. |
| Compact navigation, summary and camera-header grouping | Concepts informed later implementation. Original Parameters/Diagnostics/Settings destinations and summary composition were superseded; they are not the Current page specification. |
| Summary with PLC state and aggregate camera connectivity | Prototype/design concepts only. Real camera/PLC connectivity indicators were not Current functionality or newly approved hardware requirements. |
| Channel counts and prototype timings | Illustrative fixtures. The early “future count scope” question was later answered for application channel accounting; it did not establish physical-product counting or a throughput limit. |
| Thin segmentation contour, hollow size/position boxes, corner brackets and marker/leader alternatives | Exploratory low-occlusion specimens. Historical contour overlays were not the Current Production overlay capability. Available Current result presentation is owned by UI shell. |
| Dashed expected ROI + X for missing product | A historical presence-overlay idea, not an implemented independent Presence feature or a new acceptance rule. |
| Expandable Operator Event Bar with warning/fault rows | Superseded by the later shared fixed status strip. The prototype's expanding bar was not Current behavior or a newly approved feature. |
| Calm NG/local evidence versus global technical visibility | Separation informed later presentation, but specific feedback scopes/recovery rules are maintained in UI shell rather than defined by the old scenarios. |

The original source's late handoff wording that only a single-image Qt demo existed
was stale by the consolidation checkpoint. It was not repeated as a Current claim.
Likewise, obsolete “begin future PySide implementation from this handoff” instructions
were omitted; this archive does not initiate implementation or redefine today's UI.

## Overlay and event concepts

The prototype compared thin contour outlines, hollow rectangles/corner brackets
and compact marker-plus-leader treatments to show defect position with little
occlusion. It avoided heavy fills that could hide a physical defect. Technical
invalidity was shown in camera/header/global treatment instead of invented defect
shapes inside the image.

Those drawings were illustrative HTML fixtures, not SDK masks/contours, calibrated
channel geometry or verified defect judgments. On real images, thin red marks
could disappear at scaled viewing and amber could compete with orange capsules.
Line width, marker placement and label readability required native-size review.
A visual preference did not create missing result data or authorize invented ROIs.

The original event concept favored short operator-oriented summaries over developer
tracebacks or protocol/SDK dumps. It tried expandable one-to-three-row treatment,
then the later source annotations explicitly superseded that idea. The archived
prototype could still show Camera Warning, PLC Warning and System Fault scenarios;
those simulated states were not equipment integration or fault-recovery evidence.

## Local prototype provenance

`prototypes/production_ui/` was an ignored local Style/Layout Lab, not production
code, a runtime dependency, business logic or a required fresh-checkout/deployment
asset. Its absence would not invalidate this rationale or Current tests.
Ignored status does not authorize deleting its retained evidence in this migration.

The retained lab had static `index.html`, `styles.css`, `app.js`,
`layout-data.js`, example `tokens.css`, an asset manifest and six image copies.
It also retained an optional preparation/capture helper and a small
`review/v0.1/` set: Chinese scenario contact sheet, English NG view and an overlay
specimen HTML/PNG with four detail-image dependencies. No archive link makes
these optional ignored files a tracked documentation dependency.

The asset manifest recorded original filenames/dimensions/SHA-256. During this
consolidation, all six asset hashes matched their manifest and local source files;
the manifest and stored prototype reports described **1056 × 960** images,
uncropped, unmodified and contained at original aspect ratio. No new image decoding,
browser rendering or capture was performed.

`Capsule-A`, cycle **12842**, **285 ms** cycle timing and camera timings were
fixtures. Example NG channels **3, 17, 34**, CAM4 timeout invalidating **19–24**,
and a fault invalidating all **36** were simulated. Its counts described current-
cycle valid judgments, not lifetime/batch totals or physical capsules; abnormalities
in the photographs could be unrelated to those fabricated result scenarios.

The local README's closeout dated **2026-10-01** reported **200 combinations** and
**10 pending cases**, zero browser errors, and eight curated references matching
regenerated versions byte-for-byte. It reported prior removal of **89 obsolete
files (58.6 MiB)**, leaving **24 files (about 6.2 MiB)**. That was historical
prototype closeout, not deletion performed by this documentation batch.

The retained `outputs/production-ui-review/validation.json` independently
recorded 200 combinations, 10 pending cases, 27 generated screenshots and no
browser errors, with representative layout reports. It did not by itself prove
every closeout cleanup or byte-equivalence statement; those remained documented
outcomes. Stored reports were read only and captures were not regenerated.

## Physical review questions left open

The exploration left these matters for the actual workstation:

- Windows DPI/scaling, fullscreen/windowed behavior and title-bar/taskbar allowance.
- Installed Chinese/English font coverage, typography hierarchy and long-text visibility.
- Native-size thin-line contrast, indicator acquisition and channel mapping speed.
- Screen brightness/glare and muted-state versus abnormal-state discrimination.
- Actual touch-target dimensions, keyboard/mouse equivalence and disclosure behavior.
- Overlay/label occlusion and readability on real lighting/content.
- Ergonomic review of orientations and non-Production workspaces.

Fit screenshots assessed composition only; native-size and physical-screen testing
were needed for text, subtle contrast and touch suitability. Browser validation
and later fake Qt review did not commission that hardware or certify safety.
No new UI controls, calibrated geometry, connectivity, Presence/contour support
or expanded event bar was approved by archiving this exploration.
