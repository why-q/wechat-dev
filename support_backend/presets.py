"""Loader, dynamic overlay writer, and symmetric exact-alias matcher for `1.` preset FAQ markdown documents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

try:
    import yaml as _yaml
except ModuleNotFoundError:  # pragma: no cover - root venv without PyYAML
    _yaml = None

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]+')


def _strip_yaml_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
        return stripped[1:-1]
    if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        return stripped[1:-1].replace("''", "'")
    return stripped


def _fallback_parse_yaml_mapping(raw_yaml: str) -> dict[str, object]:
    """Minimal safe parser for flat frontmatter keys and indented dash lists."""
    parsed: dict[str, object] = {}
    current_list_key: str | None = None
    for raw_line in raw_yaml.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if current_list_key is not None and re.match(r"^[ \t]*-\s+", raw_line):
            item = re.sub(r"^[ \t]*-\s+", "", raw_line)
            target = parsed.get(current_list_key)
            if isinstance(target, list):
                target.append(_strip_yaml_quotes(item))
            continue
        current_list_key = None
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        clean_key = key.strip()
        if not clean_key:
            continue
        clean_val = value.strip()
        if not clean_val:
            parsed[clean_key] = []
            current_list_key = clean_key
        elif clean_val == "[]":
            parsed[clean_key] = []
        else:
            parsed[clean_key] = _strip_yaml_quotes(clean_val)
    return parsed


def split_frontmatter(raw_markdown: str) -> tuple[dict[str, object], str]:
    """Extract YAML frontmatter metadata dict (if present) and return (meta, body)."""
    normalized = raw_markdown.lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(normalized)
    if match is None:
        return {}, normalized.strip()
    parsed = _yaml.safe_load(match.group(1)) if _yaml is not None else _fallback_parse_yaml_mapping(match.group(1))
    meta = parsed if isinstance(parsed, dict) else {}
    body = normalized[match.end() :].strip()
    return meta, body


def normalize_alias_text(text: str) -> str:
    """Apply symmetric normalization (NFKC, collapse space, strip trailing punctuation/symbols)."""
    nfkc = unicodedata.normalize("NFKC", text)
    collapsed = " ".join(nfkc.split()).strip()
    while collapsed:
        last_cat = unicodedata.category(collapsed[-1])
        if last_cat.startswith(("P", "S", "Z", "C")):
            collapsed = collapsed[:-1]
            continue
        break
    return " ".join(collapsed.split()).strip().casefold()


def _extract_preset_id(meta: dict[str, object], md_path: Path) -> str:
    raw_id = meta.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", md_path.stem).strip("_")


def _load_presets_from_dir(corpus_dir: Path, *, is_dynamic: bool) -> list[PresetDocument]:
    corpus_path = Path(corpus_dir)
    if not corpus_path.is_dir():
        return []
    presets: list[PresetDocument] = []
    seen_ids: set[str] = set()
    for md_path in sorted(corpus_path.glob("1.*.md")):
        raw = md_path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(raw)
        if not body:
            continue
        preset_id = _extract_preset_id(meta, md_path)
        if preset_id in seen_ids:
            raise ValueError(f"Duplicate preset id: {preset_id}")
        seen_ids.add(preset_id)
        title = (
            str(meta.get("title")).strip()
            if isinstance(meta.get("title"), str) and str(meta.get("title")).strip()
            else md_path.stem.removeprefix("1.").strip()
        )
        raw_aliases = meta.get("aliases") or []
        aliases: list[str] = []
        if isinstance(raw_aliases, list):
            aliases = [
                str(item).strip()
                for item in raw_aliases
                if isinstance(item, (str, int, float)) and str(item).strip()
            ]
        applicability = (
            str(meta.get("applicability")).strip()
            if isinstance(meta.get("applicability"), str)
            else ""
        )
        presets.append(
            PresetDocument(
                preset_id=preset_id,
                title=title,
                aliases=tuple(aliases),
                applicability=applicability,
                body=body,
                source_path=md_path,
                is_dynamic=is_dynamic,
            )
        )
    return presets


def render_preset_markdown(
    *,
    preset_id: str,
    title: str,
    aliases: Sequence[str],
    applicability: str,
    body: str,
) -> str:
    """Render a `1.` preset markdown file with safe JSON-quoted YAML frontmatter."""
    clean_id = str(preset_id).strip()
    clean_title = str(title).strip()
    clean_applicability = str(applicability).strip()
    clean_body = str(body).strip()

    deduped_aliases: list[str] = []
    seen_aliases: set[str] = set()
    for raw_alias in aliases:
        alias = str(raw_alias).strip()
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        deduped_aliases.append(alias)

    lines = [
        "---",
        f"id: {json.dumps(clean_id, ensure_ascii=False)}",
        f"title: {json.dumps(clean_title, ensure_ascii=False)}",
    ]
    if deduped_aliases:
        lines.append("aliases:")
        for alias in deduped_aliases:
            lines.append(f"  - {json.dumps(alias, ensure_ascii=False)}")
    else:
        lines.append("aliases: []")
    lines.append(f"applicability: {json.dumps(clean_applicability, ensure_ascii=False)}")
    lines.append("---")
    frontmatter = "\n".join(lines)
    if clean_body:
        return f"{frontmatter}\n{clean_body}\n"
    return f"{frontmatter}\n"


def _sanitize_filename_segment(raw: str) -> str:
    text = raw.replace("..", "")
    text = _UNSAFE_FILENAME_RE.sub(" ", text)
    text = " ".join(text.split()).strip(" .")
    if len(text) > 60:
        text = text[:60].rstrip(" .")
    return text


def safe_preset_filename(title: str, preset_id: str) -> str:
    """Build a safe `1. <title>.md` filename, falling back to `preset_id` if title has no safe chars."""
    clean_title = _sanitize_filename_segment(str(title))
    if not clean_title:
        clean_title = _sanitize_filename_segment(str(preset_id))
    if not clean_title:
        clean_title = "preset"
    return f"1. {clean_title}.md"


def _atomic_write_text(target_path: Path, content: str, *, mode: int | None = None) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".preset-", suffix=".tmp", dir=target_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_stale_preset_files(directory: Path, preset_id: str, keep_path: Path) -> None:
    keep_resolved = keep_path.resolve()
    for existing in sorted(directory.glob("1.*.md")):
        if existing.resolve() == keep_resolved:
            continue
        try:
            raw = existing.read_text(encoding="utf-8")
            meta, _ = split_frontmatter(raw)
        except OSError:
            continue
        if _extract_preset_id(meta, existing) == preset_id:
            existing.unlink(missing_ok=True)


def write_dynamic_preset(
    dynamic_dir: Path,
    *,
    preset_id: str,
    title: str,
    aliases: Sequence[str],
    applicability: str,
    body: str,
) -> Path:
    """Atomically write a dynamic `1.` preset markdown file and clean stale renamed files."""
    dynamic_path = Path(dynamic_dir)
    existed = dynamic_path.exists()
    dynamic_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(dynamic_path, 0o700)

    clean_id = str(preset_id).strip()
    rendered = render_preset_markdown(
        preset_id=clean_id,
        title=title,
        aliases=aliases,
        applicability=applicability,
        body=body,
    )

    target_path = dynamic_path / safe_preset_filename(title, clean_id)
    _atomic_write_text(target_path, rendered, mode=0o600)

    # Re-parse written file to verify YAML frontmatter and body integrity.
    parsed_meta, parsed_body = split_frontmatter(target_path.read_text(encoding="utf-8"))
    if str(parsed_meta.get("id") or "").strip() != clean_id or not parsed_body:
        raise ValueError(f"Written preset failed frontmatter sanity check: {target_path}")

    _remove_stale_preset_files(dynamic_path, clean_id, target_path)
    return target_path


def export_dynamic_presets(dynamic_dir: Path, target_corpus_dir: Path) -> list[Path]:
    """Copy/sync dynamic `1.*.md` presets into a target corpus directory."""
    src_dir = Path(dynamic_dir)
    if not src_dir.is_dir():
        return []
    dst_dir = Path(target_corpus_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for src_path in sorted(src_dir.glob("1.*.md")):
        raw = src_path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(raw)
        if not body:
            continue
        preset_id = _extract_preset_id(meta, src_path)
        target_path = dst_dir / src_path.name
        _atomic_write_text(target_path, raw)
        _remove_stale_preset_files(dst_dir, preset_id, target_path)
        written.append(target_path)
    return written


@dataclass(frozen=True, slots=True)
class PresetDocument:
    """A single approved `1.` fixed answer with aliases and applicability."""

    preset_id: str
    title: str
    aliases: tuple[str, ...]
    applicability: str
    body: str
    source_path: Path
    is_dynamic: bool = False


class PresetCatalog:
    """In-memory catalog of `1.` preset answers with collision-free exact alias lookup."""

    def __init__(self, presets: list[PresetDocument]) -> None:
        self._presets: tuple[PresetDocument, ...] = tuple(presets)
        self._by_id: dict[str, PresetDocument] = {}
        self._alias_map: dict[str, PresetDocument] = {}

        for preset in self._presets:
            if preset.preset_id in self._by_id:
                raise ValueError(f"Duplicate preset id: {preset.preset_id}")
            self._by_id[preset.preset_id] = preset
            for alias in preset.aliases:
                key = normalize_alias_text(alias)
                if not key:
                    continue
                existing = self._alias_map.get(key)
                if existing is not None and existing.preset_id != preset.preset_id:
                    raise ValueError(
                        f"Normalized alias {key!r} maps to multiple presets: "
                        f"{existing.preset_id} and {preset.preset_id}"
                    )
                self._alias_map[key] = preset

    @classmethod
    def from_corpus_dirs(
        cls,
        static_dir: Path | None,
        dynamic_dir: Path | None = None,
    ) -> "PresetCatalog":
        static_presets = (
            _load_presets_from_dir(Path(static_dir), is_dynamic=False)
            if static_dir is not None
            else []
        )
        dynamic_presets = (
            _load_presets_from_dir(Path(dynamic_dir), is_dynamic=True)
            if dynamic_dir is not None and Path(dynamic_dir).is_dir()
            else []
        )
        dynamic_by_id = {preset.preset_id: preset for preset in dynamic_presets}

        merged: list[PresetDocument] = []
        replaced_ids: set[str] = set()
        for preset in static_presets:
            override = dynamic_by_id.get(preset.preset_id)
            if override is not None:
                merged.append(override)
                replaced_ids.add(preset.preset_id)
            else:
                merged.append(preset)

        for preset in dynamic_presets:
            if preset.preset_id not in replaced_ids:
                merged.append(preset)

        return cls(merged)

    @classmethod
    def from_corpus_dir(cls, corpus_dir: Path) -> "PresetCatalog":
        return cls.from_corpus_dirs(corpus_dir, None)

    @property
    def presets(self) -> tuple[PresetDocument, ...]:
        return self._presets

    def get_by_id(self, preset_id: str) -> PresetDocument | None:
        return self._by_id.get(preset_id)

    def match_exact(self, question: str) -> PresetDocument | None:
        """Return matching preset only on whole-message normalized exact alias hit."""
        key = normalize_alias_text(question)
        if not key:
            return None
        return self._alias_map.get(key)
