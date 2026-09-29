# AGENTS.md

## Project

This project is an engineering-validation demo for local ViMo industrial
vision inference.

Current target:

- Windows 10
- Python 3.14.7
- RTX 4060 Ti 16 GB
- ViMo SDK 3.20.0.2
- PySide6
- OpenCV
- NumPy
- One inspection batch contains 6 images

## Development principles

- Keep changes small and independently testable.
- Prefer simple, readable implementations over abstraction-heavy designs.
- Do not modify vendor-provided SDK examples or binaries unless explicitly asked.
- Do not install or upgrade CUDA, cuDNN, TensorRT, or GPU-related packages
  without explicit approval.
- Do not add dependencies unless they are clearly necessary.
- ViMo inference must never block the GUI main thread.
- Separate core inference logic from UI code.
- Keep the future control UI and read-only web dashboard decoupled from the
  inference core.
- Do not implement camera, PLC, database, authentication, or remote-management
  features unless explicitly requested.

## Repository structure

- `src/vimo_demo/`: typed inference core; public API is VimoSegmentationEngine.
- `tests/`: unit/integration tests and the tracked golden `reference/phase0.json`.
- `models/` and `images/`: local input assets, ignored by Git.
- `scripts/`: maintained engineering tools using only the public inference API.
- `outputs/`: generated outputs; do not depend on committed contents.
- `docs/`: project notes and technical decisions.

Historical experiments are archived in Git. Keep direct ViMo SDK imports inside
the formal adapter. Preserve model parameters and the golden regression fixture.
Run Ruff, formatting and strict Pyright on `src tests scripts`; normal pytest
must remain independent of GPU availability. GPU regressions are opt-in.

## Validation

For every implementation step:

1. State what changed.
2. Give the exact command used to run/test it.
3. Run the smallest relevant test.
4. Report failures instead of hiding or working around them.
5. Do not proceed into unrelated phases automatically.
