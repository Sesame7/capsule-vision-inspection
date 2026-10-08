# Production UI design direction

UI exploration closed on 2026-10-01. This document records the agreed direction
for a future PySide6 production workstation. It is a design reference, **not a
pixel-perfect specification or evidence of an implemented Production UI**.
Production Screen v0.1 is the final local visual reference from the exploration.

## Product and display context

The target is an industrial PC mounted vertically on the machine, with an
approximately **1080 × 1920 portrait display**. Interaction includes touch, mouse
and keyboard. The primary user is a production-line operator; engineers and
technicians also use the application during setup and commissioning.

Six camera views should normally remain visible simultaneously. Enlarging a
single camera to full screen is rarely needed and should not determine the
Production page structure. Chinese and English are both required, with concise
labels and stable layouts in either language.

Final DPI, Windows scaling, title-bar/taskbar allowance, fullscreen/windowed
behavior and physical touch-target sizing require testing on the real display.

## Chosen visual direction

**iOS-inspired Industrial Dark** means mature design principles: modern
machine-vision software with a premium, refined, dark-first appearance; calm
normal states; restrained color; strong hierarchy; concise labels; icon + short
text navigation; subtle surface depth; disciplined rounded geometry; good
typography; and touch-friendly controls. It does not mean an iPhone/iPad layout.

Avoid excessive glassmorphism, decorative animation, mobile tab bars,
gesture-only controls, excessive cards, bright saturated normal-state colors
and consumer-app styling. Images and abnormal conditions should attract attention
before decoration. This remains industrial machine-vision software.

## Production page structure

The stable vertical structure is:

```text
Compact global navigation
          ↓
Compact Production summary
          ↓
2 columns × 3 rows of camera views
          ↓
Operator Event Bar
```

The camera workspace dominates. Exact heights, spacing and pixel dimensions are
not frozen. A permanent large sidebar is not preferred on the portrait screen.

### Global navigation

Use icons with short text and a subtle but clear selected-page state.

| Conceptual destination | Chinese | Likely development priority |
| --- | --- | --- |
| Production | 生产 | High |
| Parameters | 参数 | High |
| Diagnostics | 诊断 | High |
| History | 历史 | Lower |
| Settings | 设置 | Medium |

These are conceptual destinations and priorities, not a requirement to implement
every page together. Detailed non-Production page layouts remain open.

### Production summary

Keep only high-value information: machine state, current product/batch/recipe,
inspected total, NG count, cycle/processing time, PLC state and camera aggregate
state. Include optional OK or ERR counts only when useful. Do not give all fields
equal visual weight: prioritize machine state/product and important production
numbers, with connectivity and timing secondary.

A small number of quick actions may fit here: Start Inspection, Stop Inspection,
Recipe/Product. **Stop Inspection controls the application/inspection flow; its
appearance must not imply a machine emergency stop.** Safety emergency stopping
belongs to machine hardware.

Count scope (for example batch totals versus latest-cycle results), availability
and timing definitions must be established with the future production model.
The prototype's deterministic counts and timings are not business requirements.

### Camera workspace

The six real camera images are the primary content, normally in a 2 × 3 grid.
Use a compact header approximately like:

```text
CAM 1    [six channel indicators]                  5.8 ms
```

Keep camera identity, six channel-result indicators and secondary processing
time. The image starts directly below the header. Avoid repeated channel ranges,
permanent footer rows, duplicate metadata and large textual result panels.
Normal camera framing should be subtle, without bright green borders.

Preserve useful source-image content and aspect ratio; use conservative
letterboxing/cropping decisions. Evaluate against real black-background,
orange/golden capsule images, not generated placeholders.

## Channel semantics and technical validity

There are 36 stable production channels in this workstation's intended mapping:

| Camera | Channels |
| --- | --- |
| CAM 1 | 1–6 |
| CAM 2 | 7–12 |
| CAM 3 | 13–18 |
| CAM 4 | 19–24 |
| CAM 5 | 25–30 |
| CAM 6 | 31–36 |

This mapping is a product requirement, not a reason to duplicate fixed counts
throughout runtime code; preserve the project's configuration/data boundaries.

| Result state | Preferred treatment |
| --- | --- |
| Pending, disabled or no result available | Neutral/dark |
| Successful inspection, product OK | Muted green |
| Successful inspection, product NG | Red |
| Known technical invalidity or camera uncertainty | Amber |

An absent result is not an OK result. Known technical failure must remain explicit
rather than disappear into the neutral pending state. Supplement color with
shapes/icons and concise text where necessary. Avoid 36 bright green lights
dominating normal production.

Physical indicator position provides the normal channel mapping. Numbers should
be available for NG/invalid exceptions and through interaction where useful;
they need not be permanently prominent. Final visibility, touch disclosure and
mapping speed require usability testing. Essential meaning must not depend on hover.

**Product NG and technical failure must never look identical.** Product NG means
available inspection evidence indicates a defect. Technical failure means validity
is compromised, such as a missing frame, camera timeout, inference failure or
system execution problem. These dimensions can coexist: Phase 2A preserves
ERROR + NG when defect evidence exists alongside a technical failure. A future
display must preserve both, following the [production contract](production-inspection.md).

- Product NG: red affected indicator and a local defect overlay. Do not
  automatically turn the whole camera card red.
- Camera technical problem: amber header/boundary, a concise camera-level warning
  such as “Frame timeout”, and invalid/amber channel indicators as appropriate.
  Do not draw fake product defects or imply six defective capsules.
- System/global technical fault: visible in global status and clearly distinct
  from local product NG. The prototype's violet fault cue is an example, not a
  frozen color choice. Any retained stale/reference image must be identified.

## Minimally occluding defect overlays

Show where a finding occurred without covering the underlying physical defect.

| Finding | Preferred overlay concept |
| --- | --- |
| ViMo segmentation defect | Thin contour / hollow outline |
| Capsule size or position judgment | Hollow rectangle or corner brackets |
| Missing product | Dashed expected ROI + X / missing marker |
| Color judgment | Subtle ROI edge + compact status marker |
| Technical invalidity | Camera/header/global treatment, no fake image defect |

A small marker with a leader is another useful low-occlusion treatment where
precise extent is less important. Avoid large opaque or heavily filled overlays.
Line width/color, label placement and marker size remain adjustable and must be
reviewed at 100% on the physical monitor. Thin red marks can vanish in scaled
screenshots; amber can compete with orange capsules. Actual segmentation and ROI
geometry must come from production inspection results and calibrated configuration,
not lab fixtures.

## Operator Event Bar

Provide a lightweight operational event area: approximately one compact line in
normal operation, 2–3 recent relevant events for warnings, and further expansion
when a fault warrants it. Avoid large disruptive camera-grid resizing when
possible. Exact expansion/collapse behavior and area allocation remain adjustable.

Examples: “CAM4 frame timeout”, “Channel 17 NG · deformation”, “Recipe loaded”.
This is not a developer console. Python tracebacks, paths, SDK internals and
protocol dumps belong in Diagnostics. Keep operator events short and actionable.

## Tokens, typography and input

Use dark neutral backgrounds, subtle hierarchical surfaces, quiet separators,
strong primary text and softer secondary text. Keep OK muted, product NG locally
strong, warnings amber and system faults clearly distinct. Use a restrained
accent and consistent scales for spacing, radii, typography, icons and
touch-capable targets. Exact values are subject to PySide implementation and
physical-screen review; CSS tokens are examples, not a final QSS contract.

The current typography priority is:

1. Critical machine state / technical fault.
2. Key production numbers.
3. Product/batch identity.
4. Camera identity.
5. Channel/result state.
6. Processing time.
7. Event detail.

Numbers should be easy to scan; labels should not all compete equally. Use
system-safe fonts with Chinese and English coverage. Touch targets must work
with mouse/keyboard equivalents and without gesture-only or hover-only actions.

## Stable decisions

- Portrait production layout and dark modern visual direction.
- iOS-inspired industrial principles, rather than mobile-app patterns.
- Icon + short text global navigation and a compact Production summary.
- Six simultaneous camera views in a 2 × 3 workspace.
- CAM + six indicators + processing-time camera header.
- Neutral, quiet normal state with muted OK emphasis.
- Product NG separated from technical failure.
- Minimally occluding defect overlays.
- Lightweight operator Event Bar.
- Bilingual-ready design and touch-aware controls.

## Intentionally not frozen

- Exact typography sizes and summary/navigation/event-bar heights.
- Border thickness, radii and spacing values.
- Status-indicator diameter and exact number-label visibility behavior.
- Overlay line width/color, label placement and marker size.
- Exact amber/red/green shades, accent and system-fault color.
- Animation timing, if any meaningful feedback later warrants animation;
  decorative animation remains discouraged.
- Windows fullscreen/windowed behavior, DPI/scaling and OS chrome allowance.
- Physical-screen brightness/glare tuning and final touch-target dimensions.
- Detailed layouts of non-Production pages.

Refine these during real PySide implementation and physical-screen testing.
Fit-scale screenshots judge composition only; use native size to judge text,
thin lines, subtle contrast, indicator acquisition and touch suitability.

## Prototype status and development handoff

`prototypes/production_ui/` is an ignored, local exploratory Style/Layout Lab.
It is disposable visual reference, not production code, a runtime dependency,
a source of business logic or a required deployment artifact. Its README explains
the retained v0.1 screen, scenario sheet and overlay specimen. The durable
direction in this document stands alone if those local assets are absent.

Implement the future Production UI separately in PySide6, following this direction
without copying HTML geometry or simulated decisions as requirements. Preserve the
[architecture](architecture.md) and [Qt integration](qt-integration.md) boundaries:
Qt presents state and results; inspection decisions and lifecycle ownership remain
outside widgets. The current PySide window is still the single-image integration
demo. This exploration did not implement production UI or change runtime behavior.

For the main development conversation: use this document as the UI handoff; begin
future scoped implementation from its stable decisions, resolve data/count
semantics explicitly, and validate flexible details on the portrait hardware in
both languages. This closeout does not initiate that implementation.
