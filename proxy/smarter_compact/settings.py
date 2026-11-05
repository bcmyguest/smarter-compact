"""Runtime settings for the context proxy, read from environment variables."""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from .store import ASCII_WHITESPACE
from .tokens import DEFAULT_CHARS_PER_TOKEN

DEFAULT_TRIM_AT_TOKENS = 125000
# Trim thresholds by model family (a substring of the lowercased model id), used while CONTEXT_TRIM_AT_TOKENS is
# unset. Haiku's prompts over 100k tokens bill at a higher tier, so its sessions trim below it.
MODEL_TRIM_AT_TOKENS = (("haiku", 95000),)
# A trim keeps older in-turn exchanges while the request stays under this fraction of trim_at_tokens (Haiku: ~50K).
DEFAULT_TRIM_TO_FRACTION = 0.53
DEFAULT_COMPRESS_OVER_TOKENS = 4000
DEFAULT_COMPACTION_MAX_CHARS = 12000
DEFAULT_RECALL_TIMEOUT_S = 5.0
DEFAULT_HINDSIGHT_URL = "http://127.0.0.1:8888"
DEFAULT_TTL_DAYS = 30
# OpenAI-compatible embeddings base URL (POST <url>/embeddings): Ollama on the host. compose.yaml sets the
# container's view (host.docker.internal).
DEFAULT_EMBEDDING_URL = "http://127.0.0.1:11434/v1"
# Seconds per row of an embeddings call, and characters of a row's head sent (docs/context-store.md, "Vectors").
DEFAULT_EMBEDDING_TIMEOUT_S = 30.0
DEFAULT_EMBEDDING_MAX_CHARS = 4000


def _int(env, name, default):
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(env, name, default):
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _positive_float(env, name, default):
    value = _float(env, name, default)
    return value if math.isfinite(value) and value > 0 else default


def _fraction(env, name, default):
    """A float strictly between 0 and 1, else default."""
    value = _float(env, name, default)
    return value if math.isfinite(value) and 0 < value < 1 else default


def _positive_int(env, name, default):
    value = _int(env, name, default)
    return value if value > 0 else default


def _flag(env, name, default="1"):
    """True unless the variable is 0, false, no or off (or unset with a default of "0")."""
    return str(env.get(name, default) or default).strip().lower() not in ("0", "false", "no", "off")


def _trim_overrides(env):
    """JSON from CONTEXT_TRIM_AT_OVERRIDES: per-family tokens and optional default fallback."""
    raw = str(env.get("CONTEXT_TRIM_AT_OVERRIDES", "")).strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}, None
    if not isinstance(parsed, dict):
        return {}, None
    families = {}
    default = None
    for key, value in parsed.items():
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            continue
        if tokens <= 0:
            continue
        name = str(key).lower()
        if name == "default":
            default = tokens
        else:
            families[name] = tokens
    return families, default


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    # STANDING_INSTRUCTION in every request's system prompt (CONTEXT_STANDING_INSTRUCTION=0 turns it off).
    standing_instruction: bool = True
    trim_at_tokens: int = DEFAULT_TRIM_AT_TOKENS
    # MODEL_TRIM_AT_TOKENS picks the threshold, trim_at_tokens being the fallback (CONTEXT_TRIM_AT_TOKENS unset).
    trim_at_by_model: bool = False
    # A trim lands under this fraction of trim_at_tokens, 0 < x < 1 (CONTEXT_TRIM_TO_FRACTION).
    trim_to_fraction: float = DEFAULT_TRIM_TO_FRACTION
    # Kept items before the strip bound lose their tool_use/tool_result pairs, text stays (off by default; CONTEXT_STRIP_PROSE=1 turns it on).
    strip_prose: bool = False
    # Each new trim appends a mechanical progress note (edited files, build/test runs) (CONTEXT_PROGRESS_NOTE=0 turns it off).
    progress_note: bool = True
    compress_over_tokens: int = DEFAULT_COMPRESS_OVER_TOKENS
    # Characters per token for the trim estimate until the session reports usage (CONTEXT_CHARS_PER_TOKEN).
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
    compaction_max_chars: int = DEFAULT_COMPACTION_MAX_CHARS
    # Bounded provider recall in the trim pointer (CONTEXT_COMPACTION_RECALL=0 turns it off).
    compaction_recall: bool = True
    recall_timeout_s: float = DEFAULT_RECALL_TIMEOUT_S
    hindsight_url: str = DEFAULT_HINDSIGHT_URL
    context_dir: Path = Path.home() / ".local" / "share" / "smarter-compact" / "context"
    # Session files older than this are deleted at start, at most once a day; 0 keeps them (CONTEXT_STORE_TTL_DAYS).
    ttl_days: int = DEFAULT_TTL_DAYS
    # Embedding model for the session databases' vectors; empty turns vectors off (CONTEXT_EMBEDDING_MODEL).
    embedding_model: str = ""
    embedding_url: str = DEFAULT_EMBEDDING_URL
    embedding_api_key: str = ""
    # An embeddings call of n rows times out after n times this (CONTEXT_EMBEDDING_TIMEOUT_S).
    embedding_timeout_s: float = DEFAULT_EMBEDDING_TIMEOUT_S
    # Each row's first this many characters are embedded (CONTEXT_EMBEDDING_MAX_CHARS).
    embedding_max_chars: int = DEFAULT_EMBEDDING_MAX_CHARS
    trim_families: tuple = ()
    trim_default: int = DEFAULT_TRIM_AT_TOKENS

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        # OPENMEM_STATE_DIR is the pre-rename name of the same setting and still configures it.
        state_dir = str(env.get("SMARTER_COMPACT_STATE_DIR") or env.get("OPENMEM_STATE_DIR") or "")
        state_dir = state_dir.strip(ASCII_WHITESPACE)
        if state_dir:
            root = Path(state_dir)
        else:
            root = Path.home() / ".local" / "share" / "smarter-compact"
            legacy = Path.home() / ".local" / "share" / "openmem"
            # Same fallback as contextRoot() in the plugin: a setup that never set the variable
            # keeps reading the store it already has rather than silently starting empty.
            if not root.joinpath("context").exists() and legacy.joinpath("context").exists():
                root = legacy
        families, default = _trim_overrides(env)
        return cls(
            enabled=_flag(env, "CONTEXT_PROXY_ENABLED"),
            standing_instruction=_flag(env, "CONTEXT_STANDING_INSTRUCTION"),
            trim_at_tokens=_int(env, "CONTEXT_TRIM_AT_TOKENS", DEFAULT_TRIM_AT_TOKENS),
            trim_at_by_model=_int(env, "CONTEXT_TRIM_AT_TOKENS", None) is None,
            trim_to_fraction=_fraction(env, "CONTEXT_TRIM_TO_FRACTION", DEFAULT_TRIM_TO_FRACTION),
            strip_prose=_flag(env, "CONTEXT_STRIP_PROSE", "0"),
            progress_note=_flag(env, "CONTEXT_PROGRESS_NOTE"),
            compress_over_tokens=_int(env, "CONTEXT_COMPRESS_OVER_TOKENS", DEFAULT_COMPRESS_OVER_TOKENS),
            chars_per_token=_positive_float(env, "CONTEXT_CHARS_PER_TOKEN", DEFAULT_CHARS_PER_TOKEN),
            compaction_max_chars=_int(env, "CONTEXT_COMPACTION_MAX_CHARS", DEFAULT_COMPACTION_MAX_CHARS),
            compaction_recall=_flag(env, "CONTEXT_COMPACTION_RECALL"),
            recall_timeout_s=_float(env, "CONTEXT_RECALL_TIMEOUT_S", DEFAULT_RECALL_TIMEOUT_S),
            hindsight_url=(str(env.get("HINDSIGHT_URL", "")).strip() or DEFAULT_HINDSIGHT_URL).rstrip("/"),
            context_dir=root / "context",
            ttl_days=_int(env, "CONTEXT_STORE_TTL_DAYS", DEFAULT_TTL_DAYS),
            embedding_model=str(env.get("CONTEXT_EMBEDDING_MODEL", "")).strip(),
            embedding_url=(str(env.get("CONTEXT_EMBEDDING_URL", "")).strip() or DEFAULT_EMBEDDING_URL).rstrip("/"),
            embedding_api_key=str(env.get("CONTEXT_EMBEDDING_API_KEY", "")).strip(),
            embedding_timeout_s=_positive_float(env, "CONTEXT_EMBEDDING_TIMEOUT_S", DEFAULT_EMBEDDING_TIMEOUT_S),
            embedding_max_chars=_positive_int(env, "CONTEXT_EMBEDDING_MAX_CHARS", DEFAULT_EMBEDDING_MAX_CHARS),
            trim_families=tuple(families.items()),
            trim_default=default if default is not None else DEFAULT_TRIM_AT_TOKENS,
        )

    def _merged_trim_families(self):
        merged = dict(MODEL_TRIM_AT_TOKENS)
        merged.update(dict(self.trim_families))
        return merged.items()

    def trim_at_for(self, model):
        """The trim threshold for a request to model (its id, or None)."""
        if self.trim_at_by_model and isinstance(model, str):
            name = model.lower()
            for family, tokens in self._merged_trim_families():
                if family in name:
                    return tokens
            return self.trim_default
        return self.trim_at_tokens
