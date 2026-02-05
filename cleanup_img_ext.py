#!/usr/bin/env python3
"""One-time cleanup: rename .img files to real extensions and update Markdown refs."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Dict, Optional
import re


def detect_ext(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()[:12]
    except Exception:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--export-root", default="export", help="Export root (default: export)")
    p.add_argument("--debug", action="store_true", help="Debug output")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.export_root)
    if not root.exists():
        print(f"Export root not found: {root}")
        return 1

    # Map old relative path -> new relative path
    rewrites: Dict[str, str] = {}

    for img in root.rglob("*.img"):
        ext = detect_ext(img)
        if not ext:
            continue
        new_path = img.with_suffix(ext)
        try:
            img.rename(new_path)
        except Exception:
            continue
        old_rel = os.path.relpath(img, root).replace(os.sep, "/")
        new_rel = os.path.relpath(new_path, root).replace(os.sep, "/")
        rewrites[old_rel] = new_rel

    # Update Markdown files (also fix stale .img references even if files already renamed)
    md_files = list(root.rglob("*.md"))
    ext_candidates = [".png", ".jpg", ".jpeg", ".gif", ".webp"]
    updated_files = 0
    # Find any .img path inside markdown (relative paths)
    pattern = re.compile(r'(?P<path>(?:\.\./|\./)?[^\s"\'<>]+\.img)')
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        updated = text
        # First apply known rewrites
        for old_rel, new_rel in rewrites.items():
            updated = updated.replace(old_rel, new_rel)
            updated = updated.replace("../" + old_rel, "../" + new_rel)
            updated = updated.replace("./" + old_rel, "./" + new_rel)
        # Then fix any remaining .img references by checking the filesystem
        # Replace any .img paths by checking filesystem near the markdown file
        matches = set(pattern.findall(updated))
        if args.debug:
            print(f"[DEBUG] {md}: {len(matches)} .img refs")
        for rel_path in matches:
            candidate_base = (md.parent / rel_path).resolve()
            if args.debug:
                print(f"[DEBUG] resolve {rel_path} -> {candidate_base}")
            replaced = False
            for ext in ext_candidates:
                cand = candidate_base.with_suffix(ext)
                if cand.exists():
                    new_rel = rel_path[:-4] + ext
                    updated = updated.replace(rel_path, new_rel)
                    replaced = True
                    if args.debug:
                        print(f"[DEBUG] match -> {cand}")
                    break
            if replaced:
                continue
            # Fallback: match by numeric prefix (e.g., 001_) in same directory
            try:
                prefix = candidate_base.name.split("_", 1)[0] + "_"
                dir_path = candidate_base.parent
                for file in dir_path.iterdir():
                    if file.is_file() and file.name.startswith(prefix) and file.suffix.lower() in ext_candidates:
                        new_rel = str(Path(rel_path).with_name(file.name)).replace("\\", "/")
                        updated = updated.replace(rel_path, new_rel)
                        if args.debug:
                            print(f"[DEBUG] prefix match -> {file}")
                        break
            except Exception:
                pass
        if updated != text:
            md.write_text(updated, encoding="utf-8")
            updated_files += 1

    if rewrites:
        print(f"Renamed {len(rewrites)} files and updated Markdown references in {updated_files} files.")
    else:
        print(f"No .img files renamed. Updated Markdown references in {updated_files} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
