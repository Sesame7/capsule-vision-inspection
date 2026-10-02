# Synthetic configuration examples

These schema-v1 files are test data, **not calibrated customer/machine settings**.
The two model paths and zero SHA-256 values are placeholders: artifact validation
must fail unless replaced with real, verified metadata. Parsing needs no model.

- `machine.json`: six cameras, six slots each, stable channels 1–36. ROIs use a
  synthetic 60 × 20 image coordinate system.
- `general.json`, `special.json`: separate model definitions with portable paths.
- `defaults.json`: Product A uses caller-selected model and common check settings.
- `overrides.json`: Product B selects `special`, changes CAM3 blob maximum, then
  overrides CH17 maximum/ROI and filters 黑点 for that channel only.

Presence/blob are explicitly enabled to demonstrate configuration, not because
an algorithm or production threshold is ready. Color is disabled. ViMo remains
enabled/required. No inference or SDK parameter changes occur when loading files.
