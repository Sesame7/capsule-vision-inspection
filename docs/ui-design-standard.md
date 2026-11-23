# UI design standard

Design each screen so an operator can recognize its state, find the relevant
control and predict what it will do. Apply this standard to authorized changes
in Qt and the read-only Web observer. An approved redesign may replace conventions
from versions never formally deployed. For current behavior and permissions, see
[architecture](architecture.md#display-and-web), [configuration](configuration.md)
and [communication](communication.md).

## Visible text and actions

- First consider which words or controls can be removed while the operator can
  still understand and complete the task. Remove duplicate actions, redundant
  headings, defensive copy and instructions that repeat an obvious step. Keep
  consequences that affect a decision.
- Put an action beside its target. Use the same familiar icon for the same
  action. Refresh beside a list can use an icon alone. Keep a short visible
  label when the action, target, unit or state could be misunderstood.
  A register write needs a verb beside its address and value.

## State and feedback

- Show the state reported by the owner. Distinguish saved settings from active
  settings when they differ. Keep a product rejection, a technical error and a
  Production fault distinguishable.
- Tie feedback to the operation that produced it. On failure, say what failed,
  how it affects the task and what the operator can do next, if anything. Keep
  optional technical explanations in existing details or diagnostics available
  to that role.

## Layout and disclosure

- Give evidence and frequent actions enough room to read and use comfortably.
  Keep related controls together and use gaps to separate tasks. Frequently
  used cards can stay expanded when space permits. Reserve disclosure mainly
  for advanced settings, occasional actions and secondary detail.
- Reuse the [Qt style](../src/capsule_vision/ui/style.py) for spacing, touch
  targets, themed icons and interaction states. Keep navigation and primary
  actions in stable positions across languages and state changes. When space
  is tight, wrap related controls together and keep labels with their values.

## Localization and accessibility

Use the existing catalog for complete phrases. Preserve stored names and
identifiers. Give shortened captions and icon-only actions full localized
accessible names. Preserve touch and keyboard access, visible focus and
selection in both themes. Make decision-critical information available without
hover. When text is elided, retain the full value in its tooltip and accessible
text.
