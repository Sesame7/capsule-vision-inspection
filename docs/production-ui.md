# Production UI — Phase 2D-1

The Production page is implemented. Phase 2D-2A adds the
[Parameters Recipe editor](recipe-configuration-ui.md); Diagnostics, History and
Settings remain localized placeholders. This is a simulated workstation shell,
not commissioned camera/PLC software. The [design direction](production-ui-direction.md)
remains a visual reference, not an exact implementation specification.

The [graphical ROI editor](graphical-roi-editor.md) now opens from Parameters /
Channel. It retains a frozen display reference and shares the image transform;
Production cards, result matching and application lifecycle are unchanged.

## Boundaries and updates

```text
ProductionApplication -> immutable snapshot / command Future -> QtProductionBridge
                                                               -> ProductionWindow
accepted source Frame -> LatestDisplayFrameStore -> CameraCard / CameraViewport
```

`ProductionApplication` remains authoritative for activation, lifecycle, counters,
faults and results. Widgets/bridge do not control coordinators, workers or engines.
The demo composes the real application/runtime with a fake engine; its source
thread submits cycles independently of Qt rendering. No lower-layer behavior was
changed for this page.

The GUI-thread bridge polls snapshots every **100 ms**, configurable via `poll_ms`.
Only changed snapshots or display revisions emit notifications. Start/Stop/Close
Futures use the existing destruction-safe `FutureDelivery` gate and explicit queued
Qt connections. Callbacks read only completed Futures, never mutate widgets.
Prepared Recipe selection, Batch and Reset call cheap application operations;
there is no parsing, model IO or inference on the GUI thread. Unchanged display
revisions do not trigger pixel conversions. Polling may skip intermediate display
results; authoritative counters and result delivery do not depend on painting.
Parameters separately validates saved candidate artifacts on one preparation
thread before application configuration; it never constructs an engine.

## Display ownership and identity

`LatestDisplayFrameStore` is Qt-independent and thread-safe. It retains one frame
per configured camera (six in this shell), with no history, masks or SDK objects.
Publishing copies pixels into immutable bytes-backed uint8 BGR storage. Producer
buffers must remain immutable during copying **and** throughout their independent
inference lease; an admission acknowledgement is not the end of that lease.
Older/equal `(generation, cycle_id, sequence)` publications are ignored. This
assumes increasing cycle IDs within a runtime generation, as in the source demo.

Cards retain the latest display copy and an independent `QImage.copy()` using
`Format_BGR888`; Qt painting cannot read recycled producer pixels. One conversion
occurs per changed frame revision. Store snapshots retained by external consumers
extend the old copies' lifetime; consumers must not build an unbounded history.

Indicators and overlays require a match on application generation, cycle ID,
complete FrameId (camera and sequence included), and image shape. A newer image
immediately suppresses the old overlay/indicators until its matching result arrives.
Previous-generation images are labeled reference images. Matching results use
their own camera/slot layout, even if a different candidate Recipe is selected.
Business results remain image-free.

## Presentation contract

The same six card instances rearrange only when grid mode changes:

| Window, logical pixels | Layout |
| --- | --- |
| 1080 x 1920, primary | 2 columns x 3 rows |
| 1920 x 1080 | 3 columns x 2 rows |
| 900 x 1200 | 2 columns x 3 rows |

The breakpoint is width >= 1.2 x height. Minimum size is 880 x 720; this prevents
extreme shrinking and is not a claim that every minimum-size orientation is an
operator target. Qt layouts and logical pixels handle DPI; no physical-pixel
rescaling is performed. Navigation and critical status icons are Qt-drawn shapes
with common DPR variants, independent of font glyphs. Text uses installed
Microsoft YaHei UI / Segoe UI fallbacks; no fonts are bundled or installed.

All core Chinese/English strings and visual tokens live in `production_style.py`.
The top navigation uses icons plus short text and a language switch. Cards have
camera identity, six configured-slot indicators, available measured timing and an
image, without a permanent footer. OK is a quiet green check, NG a red cross,
technical/UNKNOWN an amber warning, disabled a neutral dash, pending an outline
circle. ERROR + NG preserves both meanings. Abnormal channels show their IDs;
clicking an indicator reveals/hides its ID, and accessible names/tooltips describe
the full state. Slot positions derive from configuration, not inferred global order.

Images preserve aspect ratio and center within letterboxing. The same pure
`ImageTransform` maps existing image-space bounding rectangles into the destination
rectangle. QPainter draws clipped hollow red boxes separately from the bitmap;
no masks, contours or invented geometry are added to domain results. Camera
technical errors receive amber treatment; application FAULT has distinct text/color.
Neither is converted into invented product NG evidence.

The summary shows state, **Selected** and **Active** Recipe/model identities,
optional Batch ID, **Completed cycles / 完成周期**, **Channel OK occurrences /
通道 OK 次数**, **Channel NG occurrences / 通道 NG 次数**, and available last-inspection
elapsed time. These are channel judgment occurrences, **not physical product or
capsule counts**. Timing in the demo measures fake execution, not GPU performance.
Unknown/error totals become visible in the status area when nonzero.

| Application state | Enabled production controls |
| --- | --- |
| UNCONFIGURED | Recipe |
| READY | Start, Recipe, Batch |
| STARTING | Stop |
| RUNNING | Stop |
| STOPPING | None |
| FAULT | Reset only after runtime ownership is released |
| CLOSED | None |

This mapping is centralized in `production_presentation.controls`; application
validation remains authoritative. Explicit Batch confirmation resets counters;
blank ID means None. Stop/Start and Recipe selection do not reset counters.
The bounded status area shows current state/result/issue only, with no persistent
event history, traceback, internal path or raw vendor error.

## Closing

Close first resolves any dirty Parameters draft and waits asynchronously for any
outstanding Recipe preparation. Once approved, it ignores the initial window
event, disables operational controls and requests
application close once. Qt remains responsive. Successful Future completion is
followed by nonblocking `application.join(0)` observation before the window closes.
The default 30-second bridge timeout reports continued waiting and retains
ownership; it does not cancel native work or claim success. Cleanup failure leaves
the window open, shows the application's actual state and permits an explicit
close retry. Reset remains unavailable while runtime ownership is unsafe.
Detaching/destruction disables future delivery before emitter deletion. Launcher
fallback joins occur only after the Qt event loop exits.

## Equipment-free review

From the repository root:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920 --scenario ng
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080 --scenario warning
uv run --no-sync python scripts/run_production_ui.py --language zh --size 900x1200 --scenario fault
```

Click Start; warm-up and cleanup have small fake delays so STARTING/STOPPING remain
observable. Scenarios are normal, product NG, image-processing warning and terminal
runtime fault. Default images are synthetic; GPU, private images and ViMo are not
required. Optional `--frames-dir images` uses the first six deterministically sorted
local images, still with fake inference. A tiny validated fake artifact lives only
under ignored `outputs/production_ui_demo/`; it is not a ViMo model. No source
images are modified and no cycle archive is written.

Focused tests use fake sessions and the real application/runtime with fake engines:

```powershell
uv run --no-sync pytest tests/unit/test_production_presentation.py tests/integration/test_production_ui.py tests/unit/test_qt_integration.py --tb=short
```

Offscreen tests cover both languages at all three target sizes, geometry, identity,
BGR ownership, stale-overlay suppression, configured slot mapping, command delivery,
responsive startup, deferred shutdown, cleanup failure and all four demo scenarios.
Windows offscreen tests may read existing system fonts for realistic metrics;
they do not copy/install them. See [AGENTS](../AGENTS.md) for full quality gates.

Physical portrait-monitor validation remains necessary for Windows 100/125/150%
scaling, viewing distance, touch targets and brightness/contrast. Offscreen review
does not establish those ergonomics or camera throughput. The remaining pages,
Phase 2E archive/events/diagnostics, and Phase 3 hardware integration
remain out of scope.
