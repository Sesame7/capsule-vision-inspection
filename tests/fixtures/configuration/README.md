# Synthetic configuration fixtures

These small schema-v1 files exercise persistence and resolution, not calibration.
Model paths and zero hashes are placeholders, not deployable model metadata.

- `general.json` / `special.json`: independent model identities.
- `machine.json`: six cameras and 36 mapped ROIs; intentionally small test geometry.
- `defaults.json`: Product A uses default enabled/required ViMo, all labels.
- `overrides.json`: Product B selects `special`, sets CAM3 ViMo optional,
  then CH17 required with 黑点 filtered. This exercises default/camera/channel
  precedence without changing MachineLayout geometry.

Capsule Size/Color are omitted and therefore disabled; focused capsule tests provide
explicit synthetic settings and two-view images. None of these fixtures certify
accuracy, readiness, industrial tolerances or production geometry.
