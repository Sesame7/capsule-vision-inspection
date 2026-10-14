"""Real ViMo Production UI: saved per-camera folders or explicit CLI images."""

import argparse
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication

from vimo_demo.application.folder_replay import folder_images
from vimo_demo.communication import FinsAddress
from vimo_demo.communication.reporting import LocalReportingSettings
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.local_production import SourceNotifications as SourceNotifications
from vimo_demo.ui.local_production import compose as compose
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_window import ProductionWindow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--machine-config", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument(
        "--sources-config",
        type=Path,
        help="Default: camera_sources.json beside machine config",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--frames-dir",
        type=Path,
        help="First six naturally sorted files; overrides saved folders for this process",
    )
    selection.add_argument(
        "--images", nargs=6, type=Path, help="Six paths in configured camera order"
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--size", default="1080x1920")
    parser.add_argument(
        "--report-first-station",
        action="store_true",
        help="Opt-in automatic W49..W52 reporting to exactly 127.0.0.1",
    )
    parser.add_argument("--fins-port", type=int, default=9600)
    parser.add_argument("--fins-timeout", type=float, default=1.0)
    parser.add_argument(
        "--fins-pending",
        type=int,
        default=8,
        help="Pending report limit, excluding the active report",
    )
    parser.add_argument(
        "--fins-destination",
        nargs=3,
        type=int,
        default=(0, 2, 0),
        metavar=("NETWORK", "NODE", "UNIT"),
    )
    parser.add_argument(
        "--fins-source",
        nargs=3,
        type=int,
        default=(0, 1, 0),
        metavar=("NETWORK", "NODE", "UNIT"),
    )
    args = parser.parse_args()
    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
        if width < 880 or height < 720:
            raise ValueError("Minimum size is 880x720 logical pixels")
        paths = (
            folder_images(args.frames_dir)[:6]
            if args.frames_dir
            else (args.images or ())
        )
        composition = compose(
            args.model_config,
            args.machine_config,
            args.recipe,
            paths,
            observe_source=lambda future: delivery.watch("folder_source", future),
            sources_file=args.sources_config,
            reporting=LocalReportingSettings(
                port=args.fins_port,
                timeout=args.fins_timeout,
                max_pending=args.fins_pending,
                destination=FinsAddress(*args.fins_destination),
                source=FinsAddress(*args.fins_source),
            )
            if args.report_first_station
            else None,
            replay_controls=True,
        )
    except Exception:
        traceback.print_exc()
        return 1
    # Start cannot be requested until the gate/window exist. Initial artifact IO
    # above precedes Qt creation, and subsequent preparation uses the existing UI.
    qt = QApplication(sys.argv[:1])
    delivery = FutureDelivery()
    try:
        bridge = QtProductionBridge(
            composition.application,
            composition.store,
            local_replay=composition.application,
        )
        SourceNotifications(bridge, delivery)  # QObject parent retains the receiver.
        window = ProductionWindow(
            bridge,
            (composition.choice,),
            language="zh" if args.language == "zh" else "en",
            recipe_editor=composition.editor,
        )
        window.setWindowTitle(
            "ViMo · Local files / real inference · 本地图片 / 真实推理"
        )
        window.resize(width, height)
        window.show()
        return qt.exec()
    finally:
        # Outside the GUI loop; never release ownership while native work lives.
        composition.application.close().result()
        composition.application.join()
        delivery.detach()


if __name__ == "__main__":
    raise SystemExit(main())
