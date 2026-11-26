"""Loader and symmetric exact-alias matcher for `1.` preset FAQ markdown documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

try:
    import yaml as _yaml
except ModuleNotFoundError:  # pragma: no cover - root venv without PyYAML
    _yaml = None

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _strip_yaml_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
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


@dataclass(frozen=True, slots=True)
class PresetDocument:
    """A single approved `1.` fixed answer with aliases and applicability."""

    preset_id: str
    title: str
    aliases: tuple[str, ...]
    applicability: str
    body: str
    source_path: Path


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
    def from_corpus_dir(cls, corpus_dir: Path) -> "PresetCatalog":
        corpus_path = Path(corpus_dir)
        if not corpus_path.is_dir():
            return cls([])
        presets: list[PresetDocument] = []
        for md_path in sorted(corpus_path.glob("1.*.md")):
            raw = md_path.read_text(encoding="utf-8")
            meta, body = split_frontmatter(raw)
            if not body:
                continue
            raw_id = meta.get("id")
            preset_id = (
                str(raw_id).strip()
                if isinstance(raw_id, str) and raw_id.strip()
                else re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", md_path.stem).strip("_")
            )
            title = (
                str(meta.get("title")).strip()
                if isinstance(meta.get("title"), str) and str(meta.get("title")).strip()
                else md_path.stem.removeprefix("1.").strip()
            )
            raw_aliases = meta.get("aliases") or []
            aliases: list[str] = []
            if isinstance(raw_aliases, list):
                aliases = [str(item).strip() for item in raw_aliases if isinstance(item, (str, int, float)) and str(item).strip()]
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
                )
            )
        return cls(presets)

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
