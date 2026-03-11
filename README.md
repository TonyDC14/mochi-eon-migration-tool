# Mochi → EON Migration Tool

Converts `.mochi` flashcard decks to `.eon` format.

## Requirements

- Python 3.10+
- `pyyaml`

```bash
pip install pyyaml
```

## Usage

```bash
python mochi_to_eon.py "Módulo A.mochi"                        # → Módulo A.eon
python mochi_to_eon.py "Módulo A.mochi" -o output.eon           # custom output path
python mochi_to_eon.py "Módulo B.mochi" -n "My Deck Name"      # custom deck name
```

## What gets converted

| Mochi | EON |
|---|---|
| Text cards (`---` separator) | `NORMAL` (front/back) |
| Diagram cards (image + cloze boxes) | `DIAGRAM` (image + diagramBoxes) |
| Deck hierarchy (`parent-id`) | Nested `subDecks` |
| Image attachments | `images/` directory |

Unsupported Mochi features (cloze text deletions, SRS history) are skipped.
