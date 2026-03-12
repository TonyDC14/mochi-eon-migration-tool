#!/usr/bin/env python3
"""
Mochi → EON Migration Tool
===========================
Converts .mochi flashcard decks to .eon knowledge base format.

Usage:
    python mochi_to_eon.py <input.mochi> [--output <output.eon>] [--name <kb_name>]

If --output is not specified, the output file will have the same base name with .eon extension.
If --name is not specified, it will be derived from the deck name.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# YAML Representer tweaks – keep output readable and EON-compatible
# ---------------------------------------------------------------------------

def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    """Use block-style for multiline strings, single-quoted for the rest."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    # Let PyYAML pick the best style (plain when safe, quoted when needed)
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, _str_representer)


# ---------------------------------------------------------------------------
# Mochi data helpers
# ---------------------------------------------------------------------------

def _get(obj: dict, key: str, default=None):
    """Get a value from a Mochi transit-encoded dict (keys start with ~:)."""
    return obj.get(f"~:{key}", default)


def _get_list(obj: dict, key: str) -> list:
    """Get a list from Mochi transit ~#list wrapper."""
    val = _get(obj, key, {})
    if isinstance(val, dict):
        return val.get("~#list", [])
    if isinstance(val, list):
        return val
    return []


def _get_set(obj: dict, key: str) -> list:
    """Get a set from Mochi transit ~#set wrapper."""
    val = _get(obj, key, {})
    if isinstance(val, dict):
        return val.get("~#set", [])
    if isinstance(val, list):
        return val
    return []


def _get_timestamp(obj: dict, key: str) -> int | None:
    """Extract a timestamp (epoch ms) from a Mochi datetime wrapper."""
    val = _get(obj, key)
    if val is None:
        return None
    if isinstance(val, dict) and "~#dt" in val:
        return val["~#dt"]
    if isinstance(val, (int, float)):
        return int(val)
    return None


# ---------------------------------------------------------------------------
# Parse Mochi content into front / back
# ---------------------------------------------------------------------------

_IMAGE_MD_RE = re.compile(r"!\[.*?\]\(([^)]+)\)")


def _parse_card_content(content: str) -> tuple[str, str, list[str]]:
    """
    Split Mochi card content by the first ``---`` separator.

    Returns (front, back, referenced_images).
    Images embedded as ``![alt](filename.png)`` are collected but *removed*
    from the text because EON stores images differently.
    """
    images: list[str] = _IMAGE_MD_RE.findall(content)

    # Strip image references from text (EON doesn't inline them)
    clean = _IMAGE_MD_RE.sub("", content).strip()

    if "\n---\n" in clean:
        parts = clean.split("\n---\n", 1)
        front = parts[0].strip()
        back = parts[1].strip()
    elif clean.startswith("---\n"):
        front = ""
        back = clean[4:].strip()
    else:
        front = clean
        back = ""

    return front, back, images


# ---------------------------------------------------------------------------
# Convert a Mochi diagram card → EON DIAGRAM card
# ---------------------------------------------------------------------------

def _convert_diagram_card(
    mochi_card: dict,
    image_counter: list[int],
    src_dir: Path,
    images_dest: Path,
) -> dict | None:
    """
    Convert a Mochi diagram card to an EON DIAGRAM card.

    Mochi stores diagram box coordinates in pixels relative to the card's
    declared width/height.  EON stores them as percentages (0-100).
    """
    diagram = _get(mochi_card, "diagram")
    if diagram is None:
        return None

    attachment = diagram.get("~:attachment") or ""
    diagram_w = diagram.get("~:width", 600)
    diagram_h = diagram.get("~:height", 400)

    clozes = diagram.get("~:clozes", {})
    if isinstance(clozes, dict):
        clozes = clozes.get("~#list", [])

    # Copy the image
    image_file_name = ""
    if attachment:
        src_image = src_dir / attachment
        if src_image.exists():
            ext = src_image.suffix or ".png"
            image_file_name = f"image_{image_counter[0]}{ext}"
            shutil.copy2(src_image, images_dest / image_file_name)
            image_counter[0] += 1

    # Convert cloze boxes → diagramBoxes (percentages)
    diagram_boxes: list[dict] = []
    for cloze in clozes:
        cx = cloze.get("~:x", 0)
        cy = cloze.get("~:y", 0)
        cw = cloze.get("~:width", 0)
        ch = cloze.get("~:height", 0)

        box = {
            "id": str(uuid.uuid4()),
            "x": round(cx / diagram_w * 100) if diagram_w else 0,
            "y": round(cy / diagram_h * 100) if diagram_h else 0,
            "width": round(cw / diagram_w * 100) if diagram_w else 0,
            "height": round(ch / diagram_h * 100) if diagram_h else 0,
            "label": "",
        }
        diagram_boxes.append(box)

    timestamp = _get_timestamp(mochi_card, "created-at") or int(time.time() * 1000)

    return {
        "id": str(uuid.uuid4()),
        "front": "",
        "back": "",
        "noteText": "",
        "type": "DIAGRAM",
        "tags": list(_get_set(mochi_card, "tags")),
        "imageFileName": image_file_name,
        "diagramBoxes": diagram_boxes,
        "createdAt": timestamp,
    }


# ---------------------------------------------------------------------------
# Convert a Mochi text card → EON NORMAL card
# ---------------------------------------------------------------------------

def _convert_normal_card(
    mochi_card: dict,
    image_counter: list[int],
    src_dir: Path,
    images_dest: Path,
) -> dict | None:
    """Convert a Mochi text-based card to an EON NORMAL card."""
    content = _get(mochi_card, "content") or ""
    name = _get(mochi_card, "name") or ""

    # Some cards embed the question text only in the card name when content
    # is blank or just an image.
    front, back, images = _parse_card_content(content)

    if not front and name and name != "Untitled card":
        front = name

    # Skip cards that have no meaningful text at all
    if not front and not back:
        return None

    # Copy referenced images (informational – not embedded in EON text cards)
    for img in images:
        src_img = src_dir / img
        if src_img.exists():
            ext = src_img.suffix or ".png"
            dest_name = f"image_{image_counter[0]}{ext}"
            shutil.copy2(src_img, images_dest / dest_name)
            image_counter[0] += 1

    timestamp = _get_timestamp(mochi_card, "created-at") or int(time.time() * 1000)

    return {
        "id": str(uuid.uuid4()),
        "front": front,
        "back": back,
        "noteText": "",
        "type": "NORMAL",
        "tags": list(_get_set(mochi_card, "tags")),
        "diagramBoxes": [],
        "createdAt": timestamp,
    }


# ---------------------------------------------------------------------------
# Build deck hierarchy
# ---------------------------------------------------------------------------

def _build_deck_tree(
    mochi_decks: list[dict],
    src_dir: Path,
    images_dest: Path,
    image_counter: list[int],
) -> list[dict]:
    """
    Convert the flat Mochi deck list (with ~:parent-id references) into the
    nested EON deck tree (with subDecks).
    """
    # Index decks by their Mochi ID
    deck_map: dict[str, dict] = {}  # mochi_id → eon_deck
    children_map: dict[str | None, list[str]] = {}  # parent_mochi_id → [child_mochi_ids]
    parent_map: dict[str, str | None] = {}  # mochi_id → parent_mochi_id
    sort_map: dict[str, int] = {}  # mochi_id → sort value
    mochi_id_order: list[str] = []

    for md in mochi_decks:
        mid = _get(md, "id") or ""
        parent_mid = _get(md, "parent-id")
        name = _get(md, "name") or "Unnamed Deck"
        sort_value = _get(md, "sort")
        if sort_value is None:
            sort_value = 999999  # Put unsorted decks at the end

        # Convert cards
        mochi_cards = _get_list(md, "cards")
        eon_cards: list[dict] = []
        for mc in mochi_cards:
            if _get(mc, "archived?"):
                continue
            if _get(mc, "diagram"):
                card = _convert_diagram_card(mc, image_counter, src_dir, images_dest)
            else:
                card = _convert_normal_card(mc, image_counter, src_dir, images_dest)
            if card is not None:
                eon_cards.append(card)

        eon_deck: dict = {
            "id": str(uuid.uuid4()),
            "name": name,
            "cards": eon_cards,
        }

        deck_map[mid] = eon_deck
        parent_map[mid] = parent_mid
        sort_map[mid] = sort_value
        children_map.setdefault(parent_mid, []).append(mid)
        mochi_id_order.append(mid)

    # Nest children under parents (preserving order by sort field)
    for mid in mochi_id_order:
        child_ids = children_map.get(mid, [])
        if child_ids:
            sub_decks: list[dict] = []
            parent_eon_id = deck_map[mid]["id"]
            # Sort children by their sort value
            child_ids_sorted = sorted(child_ids, key=lambda cid: sort_map.get(cid, 999999))
            for cid in child_ids_sorted:
                child = deck_map[cid]
                child["parentId"] = parent_eon_id
                sub_decks.append(child)
            deck_map[mid]["subDecks"] = sub_decks

    # Collect root-level decks (those without a parent, or whose parent is not
    # in the deck list) - ordered by sort field
    roots: list[dict] = []
    root_ids: list[str] = []
    for mid in mochi_id_order:
        parent_mid = parent_map[mid]
        if parent_mid is None or parent_mid not in deck_map:
            root_ids.append(mid)
    
    # Sort roots by their sort value
    root_ids_sorted = sorted(root_ids, key=lambda mid: sort_map.get(mid, 999999))
    for mid in root_ids_sorted:
        eon_deck = deck_map[mid]
        eon_deck["parentId"] = None
        roots.append(eon_deck)

    return roots


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert_mochi_to_eon(
    mochi_path: str,
    output_path: str | None = None,
    deck_name: str | None = None,
) -> str:
    """
    Convert a .mochi file to a .eon file.

    Parameters
    ----------
    mochi_path : str
        Path to the input .mochi file (ZIP archive).
    output_path : str | None
        Path for the output .eon file.  Defaults to ``<basename>.eon``.
    deck_name : str | None
        Display name for the root deck.  Derived from decks if omitted.

    Returns
    -------
    str
        The path of the produced .eon file.
    """
    mochi_path = Path(mochi_path).resolve()
    if not mochi_path.exists():
        raise FileNotFoundError(f"Input file not found: {mochi_path}")

    # --- Determine output path ---
    if output_path is None:
        output_path = mochi_path.with_suffix(".eon")
    else:
        output_path = Path(output_path).resolve()

    # --- Extract the .mochi ZIP to a temp dir ---
    with tempfile.TemporaryDirectory(prefix="mochi2eon_") as tmpdir:
        tmp = Path(tmpdir)
        src_dir = tmp / "src"
        dst_dir = tmp / "dst"
        images_dest = dst_dir / "images"
        images_dest.mkdir(parents=True)

        # Check if the .mochi file is already extracted (user has _FILES dir)
        mochi_files_dir = Path(str(mochi_path) + "_FILES")
        if mochi_files_dir.is_dir():
            # Use the already-extracted directory
            data_json = mochi_files_dir / "data.json"
            if not data_json.exists():
                raise FileNotFoundError(
                    f"data.json not found in {mochi_files_dir}"
                )
            src_dir = mochi_files_dir
        else:
            # Extract from ZIP
            with zipfile.ZipFile(mochi_path, "r") as zf:
                zf.extractall(src_dir)

        data_json = src_dir / "data.json"
        if not data_json.exists():
            raise FileNotFoundError(f"data.json not found after extraction")

        with open(data_json, "r", encoding="utf-8") as f:
            mochi_data = json.load(f)

        mochi_decks = mochi_data.get("~:decks", [])
        if not mochi_decks:
            raise ValueError("No decks found in the Mochi file.")

        # --- Determine deck name ---
        # Use the root deck name, or the first deck without a parent
        if deck_name is None:
            for d in mochi_decks:
                if _get(d, "parent-id") is None:
                    deck_name = _get(d, "name") or "Imported Deck"
                    break
            else:
                deck_name = _get(mochi_decks[0], "name") or "Imported Deck"

        # --- Convert ---
        image_counter = [0]
        eon_decks = _build_deck_tree(mochi_decks, src_dir, images_dest, image_counter)

        # --- Build root deck structure (Tejido_Muscular.eon format) ---
        # If there is a single root deck, use it directly.
        # If there are multiple roots, wrap them under a synthetic root.
        now_ms = int(time.time() * 1000)

        if len(eon_decks) == 1:
            root_deck = eon_decks[0]
            root_deck["name"] = deck_name
        else:
            root_deck = {
                "id": str(uuid.uuid4()),
                "name": deck_name,
                "cards": [],
                "subDecks": eon_decks,
                "parentId": None,
            }
            # Set parentId on children
            for sd in eon_decks:
                sd["parentId"] = root_deck["id"]

        root_deck["parentId"] = None
        root_deck["metadata"] = {
            "exportedAt": now_ms,
            "version": "1.0.0",
        }

        # --- Write YAML ---
        yaml_path = dst_dir / "deck.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(
                root_deck,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=200,
            )

        # --- Pack into ZIP (.eon) ---
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as zf:
            zf.write(yaml_path, "deck.yaml")
            for img_file in sorted(images_dest.iterdir()):
                zf.write(img_file, f"images/{img_file.name}")

    return str(output_path)


# ---------------------------------------------------------------------------
# Statistics helper
# ---------------------------------------------------------------------------

def _count_cards_deck(deck: dict) -> tuple[int, int, int]:
    """Count total normal cards, diagram cards, and sub-decks recursively."""
    total_normal = 0
    total_diagram = 0
    total_decks = 0
    for c in deck.get("cards", []):
        if c.get("type") == "DIAGRAM":
            total_diagram += 1
        else:
            total_normal += 1
    for sd in deck.get("subDecks", []):
        total_decks += 1
        sn, sdiag, sdecks = _count_cards_deck(sd)
        total_normal += sn
        total_diagram += sdiag
        total_decks += sdecks
    return total_normal, total_diagram, total_decks


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert a .mochi flashcard deck to .eon format."
    )
    parser.add_argument("input", help="Path to the .mochi file")
    parser.add_argument(
        "--output", "-o", default=None, help="Output .eon file path"
    )
    parser.add_argument(
        "--name", "-n", default=None, help="Root deck display name"
    )

    args = parser.parse_args()

    try:
        result = convert_mochi_to_eon(args.input, args.output, deck_name=args.name)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Print summary ---
    print(f"✓ Created: {result}")

    # Quick stats from the produced file
    with zipfile.ZipFile(result, "r") as zf:
        with zf.open("deck.yaml") as yf:
            root = yaml.safe_load(yf)

    normal, diagram, sub_count = _count_cards_deck(root)
    images = sum(1 for n in zipfile.ZipFile(result).namelist() if n.startswith("images/"))
    print(f"  Sub-decks: {sub_count}")
    print(f"  Normal cards: {normal}")
    print(f"  Diagram cards: {diagram}")
    print(f"  Images: {images}")


if __name__ == "__main__":
    main()
