#!/usr/bin/env python3
"""Convert repository images to WebP and update local references."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VALIDATED_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".svg", ".ico")
PERSON_DATA_FILES = {
    "alumni_visitors.yml",
    "graduated_phd.yml",
    "phd_members.yml",
    "team_members.yml",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".htm",
    ".js",
    ".json",
    ".liquid",
    ".md",
    ".markdown",
    ".scss",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {
    ".gitignore",
    "CNAME",
    "Gemfile",
    "Gemfile.lock",
    "README",
    "README.md",
}
SKIP_DIRS = {
    ".bundle",
    ".git",
    ".jekyll-cache",
    ".sass-cache",
    "_site",
    "node_modules",
    "vendor",
}


@dataclass
class ImageChange:
    old_path: Path
    new_path: Path
    old_size: int
    new_size: int | None = None
    converted: bool = False
    deleted_original: bool = False


@dataclass
class MissingReference:
    source: Path
    line_number: int
    reference: str
    expected_path: Path


def run_git(root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def repo_root(start: Path) -> Path:
    try:
        result = run_git(start, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.CalledProcessError):
        return start.resolve()
    return Path(result.stdout.strip()).resolve()


def relative_to_root(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def import_pillow() -> tuple[object, object]:
    try:
        from PIL import Image, ImageOps, features
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to convert images. Install it with:\n"
            "  python -m pip install Pillow"
        ) from exc
    if not features.check("webp"):
        raise SystemExit("Pillow is installed, but this build does not include WebP support.")
    return Image, ImageOps


def has_alpha(image: object) -> bool:
    mode = getattr(image, "mode", "")
    info = getattr(image, "info", {})
    return mode in {"RGBA", "LA"} or (mode == "P" and "transparency" in info)


def collect_candidates(root: Path, image_dirs: list[str]) -> list[Path]:
    candidates: list[Path] = []
    for image_dir in image_dirs:
        base = (root / image_dir).resolve()
        if not base.exists():
            print(f"warning: image directory not found: {image_dir}", file=sys.stderr)
            continue
        if not base.is_dir():
            print(f"warning: not a directory: {image_dir}", file=sys.stderr)
            continue
        if not is_inside(base, root):
            raise SystemExit(f"Refusing to scan outside repository: {base}")

        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                candidates.append(path)
    return candidates


def convert_image(
    path: Path,
    root: Path,
    quality: int,
    keep_originals: bool,
    overwrite: bool,
    dry_run: bool,
    Image: object | None,
    ImageOps: object | None,
) -> ImageChange:
    old_size = path.stat().st_size
    target = path.with_suffix(".webp")
    change = ImageChange(old_path=path, new_path=target, old_size=old_size)

    if dry_run:
        return change

    should_write = overwrite or not target.exists() or target.stat().st_mtime < path.stat().st_mtime
    if should_write:
        assert Image is not None
        assert ImageOps is not None
        tmp_target = target.with_name(f"{target.name}.tmp")
        with Image.open(path) as opened:  # type: ignore[attr-defined]
            image = ImageOps.exif_transpose(opened)  # type: ignore[attr-defined]
            alpha = has_alpha(image)
            image = image.convert("RGBA" if alpha else "RGB")

            save_kwargs: dict[str, object] = {"format": "WEBP", "method": 6}
            if path.suffix.lower() == ".png" and alpha:
                save_kwargs["lossless"] = True
            else:
                save_kwargs["quality"] = quality

            image.save(tmp_target, **save_kwargs)
        tmp_target.replace(target)
        change.converted = True

    change.new_size = target.stat().st_size if target.exists() else None
    if not target.exists() or target.stat().st_size <= 0:
        raise SystemExit(f"WebP conversion failed for {relative_to_root(path, root)}")

    if not keep_originals:
        path.unlink()
        change.deleted_original = True

    return change


def can_scan_as_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES


def iter_text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        if can_scan_as_text(path):
            yield path


def decode_text(data: bytes) -> tuple[str, bool] | None:
    if b"\x00" in data:
        return None
    has_bom = data.startswith(b"\xef\xbb\xbf")
    encoding = "utf-8-sig" if has_bom else "utf-8"
    try:
        return data.decode(encoding), has_bom
    except UnicodeDecodeError:
        return None


def build_replacements(root: Path, changes: list[ImageChange]) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for change in changes:
        old_rel = relative_to_root(change.old_path, root)
        new_rel = relative_to_root(change.new_path, root)
        replacements.extend(
            [
                (old_rel, new_rel),
                (old_rel.replace("/", "\\"), new_rel.replace("/", "\\")),
                (change.old_path.name, change.new_path.name),
            ]
        )

    # Longer strings first prevents basename replacement from hiding full-path replacements.
    unique = list(dict.fromkeys(replacements))
    unique.sort(key=lambda pair: len(pair[0]), reverse=True)
    return unique


def update_references(root: Path, changes: list[ImageChange], dry_run: bool) -> list[Path]:
    replacements = build_replacements(root, changes)
    if not replacements:
        return []

    changed_files: list[Path] = []
    for path in iter_text_files(root):
        data = path.read_bytes()
        decoded = decode_text(data)
        if decoded is None:
            continue
        text, has_bom = decoded
        new_text = text
        for old, new in replacements:
            new_text = new_text.replace(old, new)
        if new_text == text:
            continue

        changed_files.append(path)
        if not dry_run:
            prefix = b"\xef\xbb\xbf" if has_bom else b""
            path.write_bytes(prefix + new_text.encode("utf-8"))

    return changed_files


def strip_inline_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    return value.split("#", 1)[0].strip()


def add_missing(
    missing: list[MissingReference],
    root: Path,
    source: Path,
    line_number: int,
    reference: str,
    expected: Path,
) -> None:
    if reference.startswith(("http://", "https://", "data:")):
        return
    if not expected.is_file():
        missing.append(MissingReference(source, line_number, reference, expected))


def validate_direct_image_paths(root: Path, missing: list[MissingReference]) -> None:
    pattern = re.compile(
        r"(?P<path>/?(?:images|assets)/[^\s'\"()<>]+?\.(?:jpe?g|png|webp|svg|ico))",
        re.IGNORECASE,
    )
    for source in iter_text_files(root):
        decoded = decode_text(source.read_bytes())
        if decoded is None:
            continue
        text, _ = decoded
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                reference = match.group("path").split("?", 1)[0].split("#", 1)[0]
                if "{{" in reference or "}}" in reference:
                    continue
                expected = root / reference.lstrip("/")
                add_missing(missing, root, source, line_number, reference, expected)


def validate_data_image_fields(root: Path, missing: list[MissingReference]) -> None:
    data_dir = root / "_data"
    if not data_dir.is_dir():
        return

    field_pattern = re.compile(r"^\s*(?P<field>photo|image):\s*(?P<value>.*)$")
    for source in data_dir.glob("*.yml"):
        if source.name in PERSON_DATA_FILES:
            base_dir = root / "images" / "teampic"
        elif source.name == "pictures.yml":
            base_dir = root / "images" / "picpic" / "Gallery"
        else:
            continue

        decoded = decode_text(source.read_bytes())
        if decoded is None:
            continue
        text, _ = decoded
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = field_pattern.match(line)
            if not match:
                continue
            value = strip_inline_value(match.group("value"))
            if not value or not value.lower().endswith(VALIDATED_IMAGE_SUFFIXES):
                continue
            if value.startswith("/") or "/" in value or "\\" in value:
                expected = root / value.lstrip("/").replace("\\", "/")
            else:
                expected = base_dir / value
            add_missing(missing, root, source, line_number, value, expected)


def validate_image_references(root: Path) -> list[MissingReference]:
    missing: list[MissingReference] = []
    validate_direct_image_paths(root, missing)
    validate_data_image_fields(root, missing)
    return missing


def fail_on_missing_references(root: Path, missing: list[MissingReference]) -> None:
    print("Missing image references found:", file=sys.stderr)
    for item in missing:
        source = relative_to_root(item.source, root)
        expected = relative_to_root(item.expected_path, root)
        print(f"  {source}:{item.line_number}: {item.reference} -> missing {expected}", file=sys.stderr)
    raise SystemExit(1)


def git_add(root: Path, paths: list[Path]) -> None:
    rels = sorted({relative_to_root(path, root) for path in paths if is_inside(path, root)})
    if not rels:
        return

    batch_size = 80
    for index in range(0, len(rels), batch_size):
        batch = rels[index : index + batch_size]
        try:
            run_git(root, ["add", "-A", "--", *batch])
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise SystemExit(f"git add failed: {detail}") from exc


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-dir",
        action="append",
        default=["images"],
        help="Directory to scan recursively. Repeatable. Default: images",
    )
    parser.add_argument("--quality", type=int, default=82, help="WebP quality for lossy images. Default: 82")
    parser.add_argument("--keep-originals", action="store_true", help="Do not delete png/jpg/jpeg originals")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without editing files")
    parser.add_argument("--no-update-refs", action="store_true", help="Do not rewrite text references to .webp")
    parser.add_argument("--no-validate-refs", action="store_true", help="Do not fail on missing local image references")
    parser.add_argument("--no-overwrite", action="store_true", help="Do not refresh existing .webp files")
    parser.add_argument("--stage", action="store_true", help="Stage converted files, deleted originals, and refs")
    parser.add_argument("--quiet", action="store_true", help="Suppress no-op output")
    parser.add_argument("--check-deps", action="store_true", help="Verify that conversion dependencies are installed")
    parser.add_argument(
        "--fail-on-change",
        action="store_true",
        help="Exit with code 2 when changes were made; useful for pre-push hooks",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check_deps:
        import_pillow()
        print("Pillow is available.")
        return 0

    root = repo_root(Path.cwd())
    candidates = collect_candidates(root, args.image_dir)

    if not candidates:
        if not args.no_validate_refs:
            missing = validate_image_references(root)
            if missing:
                fail_on_missing_references(root, missing)
        if not args.quiet:
            print("No png/jpg/jpeg files found under configured image directories.")
        return 0

    Image = ImageOps = None
    if not args.dry_run:
        Image, ImageOps = import_pillow()

    changes: list[ImageChange] = []
    for path in candidates:
        changes.append(
            convert_image(
                path=path,
                root=root,
                quality=args.quality,
                keep_originals=args.keep_originals,
                overwrite=not args.no_overwrite,
                dry_run=args.dry_run,
                Image=Image,
                ImageOps=ImageOps,
            )
        )

    ref_files: list[Path] = []
    if not args.no_update_refs:
        ref_files = update_references(root, changes, args.dry_run)

    if not args.dry_run and not args.no_validate_refs:
        missing = validate_image_references(root)
        if missing:
            fail_on_missing_references(root, missing)

    if args.stage and not args.dry_run:
        stage_paths: list[Path] = []
        for change in changes:
            stage_paths.extend([change.old_path, change.new_path])
        stage_paths.extend(ref_files)
        git_add(root, stage_paths)

    before_bytes = sum(change.old_size for change in changes)
    after_bytes = sum(change.new_size or 0 for change in changes)

    action = "Would convert" if args.dry_run else "Converted"
    print(f"{action} {len(changes)} image(s) under: {', '.join(args.image_dir)}")
    if args.dry_run:
        for change in changes:
            print(f"  {relative_to_root(change.old_path, root)} -> {relative_to_root(change.new_path, root)}")
    else:
        saved = before_bytes - after_bytes
        print(f"Original total: {format_bytes(before_bytes)}")
        print(f"WebP total:     {format_bytes(after_bytes)}")
        print(f"Space saved:    {format_bytes(saved)}")
    if ref_files:
        print(f"Updated {len(ref_files)} reference file(s).")
    if args.stage and not args.dry_run:
        print("Staged image and reference changes.")

    made_changes = (not args.dry_run) and (
        any(change.converted or change.deleted_original for change in changes) or bool(ref_files)
    )
    return 2 if args.fail_on_change and made_changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
