# Synthetic configuration fixtures

These small versionless files exercise persistence and resolution, not calibration.
Model paths and zero hashes are placeholders, not deployable model metadata.

- `models/general/model.json` / `models/special/model.json`: independent model identities.
- `machine.tsv`: six cameras and 36 mapped ROIs; intentionally small test geometry.
- `product-a.json`: Product A explicitly selects `general`, using enabled/required ViMo and all labels.
- `product-b.json`: Product B explicitly selects `special`, with the same
  unified Recipe-level enabled/required ViMo policy and all labels.
  Both Recipes resolve against the authoritative MachineLayout geometry.

Capsule Size/Color are omitted and therefore disabled; focused capsule tests provide
explicit synthetic settings and two-view images. None of these fixtures certify
accuracy, readiness, industrial tolerances or production geometry.
