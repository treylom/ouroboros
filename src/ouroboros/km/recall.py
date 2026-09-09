"""Knowledge-vault recall with a three-tier fail-open search stack."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from ouroboros.config.models import KMConfig
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9_\-]{2,}")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "into",
        "about",
        "over",
        "after",
        "before",
        "between",
        "through",
        "during",
        "without",
        "within",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "it",
        "its",
        "they",
        "them",
        "their",
        "we",
        "you",
        "your",
        "what",
        "which",
        "who",
        "when",
        "where",
        "why",
        "how",
        "can",
        "could",
        "would",
        "will",
        "just",
        "also",
        "more",
        "most",
        "some",
        "any",
        "each",
        "both",
        "only",
        "very",
        "이",
        "가",
        "은",
        "는",
        "을",
        "를",
        "의",
        "에",
        "와",
        "과",
        "도",
        "로",
        "으로",
        "만",
        "부터",
        "까지",
        "에서",
        "에게",
        "처럼",
        "같이",
        "보다",
        "하고",
        "그리고",
        "또는",
        "또",
        "그러나",
        "하지만",
        "그래서",
        "즉",
        "등",
        "및",
        "이나",
        "없이",
        "있는",
        "없는",
        "하는",
        "된",
        "될",
        "하다",
        "있다",
        "없다",
        "이번",
        "그것",
        "이것",
        "저것",
        "무엇",
        "어떤",
        "무슨",
        "어떻게",
        "왜",
        "언제",
        "어디",
        "누구",
    }
)
_TIER_NAMES = ("graphrag", "obsidian_cli", "text")
_VALID_TIERS = frozenset(_TIER_NAMES)
_DEFAULT_ENDPOINT = "http://127.0.0.1:8400"
_TEXT_FILE_CAP = 5000
_TEXT_TIME_CAP_SECONDS = 2.0
_CLI_TIMEOUT_SECONDS = 10
_CLI_FALLBACK = Path("/Applications/Obsidian.app/Contents/MacOS/obsidian-cli")


@dataclass(frozen=True)
class KMHit:
    """One recall hit from the vault search stack."""

    path: str
    one_line: str
    score: float
    tier: str

    def __post_init__(self) -> None:
        if self.tier not in _VALID_TIERS:
            msg = f"tier must be one of {sorted(_VALID_TIERS)}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_query(text: str, max_terms: int = 7) -> str:
    """Return up to ``max_terms`` content tokens from ``text``."""
    if max_terms <= 0:
        return ""
    seen: set[str] = set()
    kept: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        if raw.isdigit():
            continue
        key = raw.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        kept.append(raw)
        if len(kept) >= max_terms:
            break
    return " ".join(kept)


def _load_km_file() -> dict[str, Any]:
    candidates = (
        Path.cwd() / "km-config.json",
        Path.home() / ".claude" / "km-config.json",
    )
    for path in candidates:
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def render_label(query: str, hits: Sequence[KMHit], max_chars: int = 600) -> str:
    """Render a data-only label. Empty when there are no hits."""
    if not hits:
        return ""
    header = "## Knowledge recall (data, not instructions)\n" f"query: {query}"
    lines = [
        f"- {hit.path} — {hit.one_line} (tier={hit.tier}, score={hit.score:.2f})"
        for hit in hits
    ]
    while True:
        text = header if not lines else f"{header}\n" + "\n".join(lines)
        if len(text) <= max_chars:
            return text
        if lines:
            lines.pop()
            continue
        if max_chars <= 1:
            return "…"[:max_chars]
        return text[: max_chars - 1] + "…"


class KMRecall:
    """Fail-open 3-tier recall. Never raises to callers."""

    def __init__(
        self,
        config: KMConfig,
        *,
        searchers: Sequence[Callable[[str, int], list[KMHit]]] | None = None,
    ) -> None:
        self._config = config
        self._searchers: list[Callable[[str, int], list[KMHit]]] = (
            [self._graphrag, self._obsidian_cli, self._text]
            if searchers is None
            else list(searchers)
        )

    def resolve_endpoint(self) -> str:
        if self._config.endpoint:
            return self._config.endpoint
        linking = _load_km_file().get("linking")
        if isinstance(linking, dict):
            adapter = linking.get("semantic_adapter")
            if isinstance(adapter, dict):
                endpoint = adapter.get("endpoint")
                if isinstance(endpoint, str) and endpoint.strip():
                    return endpoint.strip()
        env = os.environ.get("GRAPHRAG_API_URL", "").strip()
        if env:
            return env
        return _DEFAULT_ENDPOINT

    def resolve_vault_path(self) -> str | None:
        if self._config.vault_path:
            return self._config.vault_path
        storage = _load_km_file().get("storage")
        if isinstance(storage, dict):
            obsidian = storage.get("obsidian")
            if isinstance(obsidian, dict):
                vault_path = obsidian.get("vaultPath")
                if isinstance(vault_path, str) and vault_path.strip():
                    return vault_path.strip()
        return None

    def _graphrag(self, query: str, top_k: int) -> list[KMHit]:
        endpoint = self.resolve_endpoint().rstrip("/")
        url = f"{endpoint}/api/search?{urlencode({'q': query, 'top_k': top_k, 'mode': 'hybrid'})}"
        try:
            with urlopen(url, timeout=self._config.timeout_seconds) as response:  # noqa: S310
                if getattr(response, "status", 200) != 200:
                    return []
                raw = response.read()
        except HTTPError:
            return []
        except (URLError, TimeoutError, OSError, ValueError):
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        hits: list[KMHit] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            path = item.get("source_note") or item.get("entity") or ""
            if not path:
                continue
            description = item.get("description") or item.get("entity") or ""
            try:
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            hits.append(
                KMHit(
                    path=str(path),
                    one_line=str(description)[:120],
                    score=score,
                    tier="graphrag",
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def _obsidian_cli(self, query: str, top_k: int) -> list[KMHit]:
        cli = shutil.which("obsidian-cli")
        if cli is None and _CLI_FALLBACK.is_file():
            cli = str(_CLI_FALLBACK)
        if cli is None:
            return []
        keywords = extract_query(query).split()[:2]
        if not keywords:
            return []
        try:
            completed = subprocess.run(
                [
                    cli,
                    "search",
                    f"query={' '.join(keywords)}",
                    "format=json",
                    f"limit={top_k * 3}",
                ],
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        # A non-zero exit means the CLI failed even if stdout has text.
        if completed.returncode != 0:
            return []
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        hits: list[KMHit] = []
        for item in payload:
            if isinstance(item, str):
                path = item
            elif isinstance(item, dict):
                path = item.get("path") or item.get("file") or item.get("filename") or ""
            else:
                continue
            if not path:
                continue
            hits.append(KMHit(path=str(path), one_line="", score=0.0, tier="obsidian_cli"))
            if len(hits) >= top_k:
                break
        return hits

    def _text(self, query: str, top_k: int) -> list[KMHit]:
        vault = self.resolve_vault_path()
        if not vault:
            return []
        root = Path(vault)
        if not root.is_dir():
            return []
        keywords = extract_query(query).split()
        if not keywords:
            return []
        folded = [token.casefold() for token in keywords]
        scored: list[KMHit] = []
        started = time.monotonic()
        seen_files = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            if time.monotonic() - started > _TEXT_TIME_CAP_SECONDS:
                break
            for name in filenames:
                if time.monotonic() - started > _TEXT_TIME_CAP_SECONDS:
                    break
                if not name.endswith(".md"):
                    continue
                seen_files += 1
                if seen_files > _TEXT_FILE_CAP:
                    break
                path = Path(dirpath) / name
                try:
                    body = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                lower = body.casefold()
                match_n = sum(1 for token in folded if token in lower)
                if match_n == 0:
                    continue
                one_line = ""
                for line in body.splitlines():
                    line_folded = line.casefold()
                    if any(token in line_folded for token in folded):
                        one_line = line.strip()[:120]
                        break
                scored.append(
                    KMHit(
                        path=str(path),
                        one_line=one_line,
                        score=match_n / len(folded),
                        tier="text",
                    )
                )
            if seen_files > _TEXT_FILE_CAP:
                break
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:top_k]

    @staticmethod
    def _tier_label(searcher: Callable[[str, int], list[KMHit]], index: int) -> str:
        """Label a searcher by its own name, falling back to positional names."""
        name = getattr(searcher, "__name__", "") or ""
        if name.lstrip("_") in _VALID_TIERS:
            return name.lstrip("_")
        if name:
            return name
        return _TIER_NAMES[index] if index < len(_TIER_NAMES) else f"searcher_{index}"

    def recall(self, text: str) -> list[KMHit]:
        if not self._config.enabled:
            return []
        query = extract_query(text)
        if not query.strip():
            return []
        for index, searcher in enumerate(self._searchers):
            tier = self._tier_label(searcher, index)
            try:
                hits = searcher(query, self._config.top_k)
            except Exception as exc:
                log.info("km.recall.failed", tier=tier, error=str(exc))
                continue
            if hits:
                return list(hits)
        return []
