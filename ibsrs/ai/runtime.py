from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Optional

from ibsrs.policy import Policy
from ibsrs.utils.io import utc_now_iso

# ---- fully optional imports (deterministic-first) -------------------------
try:  # python-dotenv
    from dotenv import load_dotenv
    load_dotenv()
    _DOTENV = True
except Exception:  # pragma: no cover - dotenv is optional
    _DOTENV = False
    def load_dotenv(*a, **k):  # type: ignore  # no-op shim so __init__ can call it
        return False

_KEY_PLACEHOLDERS = {"", "your_key_here", "sk-your_key_here", "changeme"}


def _read_api_key() -> str:
    """Re-read OPENAI_API_KEY from .env each call (override=True) so a running
    server picks up edits without a restart. Treats placeholder values as unset."""
    try:
        load_dotenv(override=True)
    except Exception:
        pass
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return "" if key in _KEY_PLACEHOLDERS else key


def _mask_key(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("(none)" if not key else "set")

try:  # openai
    from openai import OpenAI
    _OPENAI_LIB = True
except Exception:  # pragma: no cover
    _OPENAI_LIB = False

try:  # sentence-transformers (pulls torch/transformers)
    from sentence_transformers import SentenceTransformer
    _ST_LIB = True
except Exception:  # pragma: no cover
    _ST_LIB = False


class AIRuntime:
    def __init__(self, policy: Policy, run_dir: Optional[Path] = None,
                 use_ai: Optional[bool] = None):
        self.policy = policy
        self.run_dir = Path(run_dir) if run_dir else None
        self.log_path = (self.run_dir / "llm_calls.log") if self.run_dir else None
        self.call_count = 0

        # Master switch: explicit use_ai arg > policy.ai.enabled (default True).
        # Even when "on", each capability still requires its library/key.
        cfg_enabled = bool(policy.get("ai.enabled", True))
        self.enabled = cfg_enabled if use_ai is None else bool(use_ai)

        self._embedder = None            # lazy-loaded SentenceTransformer
        self._embed_cache: dict[str, list[float]] = {}
        self._client = None              # lazy-loaded OpenAI client

        self.embed_model = str(policy.get("ai.embedding_model", "all-MiniLM-L6-v2"))
        self.extraction_model = str(policy.get("ai.llm_extraction_model", "gpt-4o-mini"))
        self.reasoning_model = str(policy.get("ai.llm_reasoning_model", "gpt-4o"))
        self.max_retries = int(policy.get("ai.llm_max_retries", 2))

        # Re-read the key from .env on every run (override=True) so a running
        # server picks up a fixed/edited key WITHOUT needing a restart.
        self.api_key = _read_api_key()
        self.key_fingerprint = _mask_key(self.api_key)
        self.has_api_key = bool(self.api_key)
        self.embeddings_available = self.enabled and _ST_LIB
        self.llm_available = self.enabled and _OPENAI_LIB and self.has_api_key
        # Filled in the first time an LLM call fails (e.g. invalid key / no
        # credits / offline) so the UI/audit can report *why* it fell back.
        self.llm_error: Optional[str] = None

        if self.log_path:  # always create the audit log, even for pure rule runs
            header = (f"# IBSRS LLM/AI call log\n"
                      f"# started: {utc_now_iso()}\n"
                      f"# ai_enabled={self.enabled} embeddings={self.embeddings_available} "
                      f"llm_key_configured={self.llm_available} key={self.key_fingerprint} "
                      f"(dotenv={_DOTENV} openai_lib={_OPENAI_LIB} st_lib={_ST_LIB} "
                      f"key_present={self.has_api_key})\n"
                      f"# note: llm validity is confirmed on the first call below\n")
            self.log_path.write_text(header, encoding="utf-8")

    def _note_llm_failure(self, exc: Exception) -> None:
        """Record the failure reason and, for auth/quota errors, stop trying
        the dead key for the rest of this run (avoids slow repeated retries)."""
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "invalid_api_key" in low or "incorrect api key" in low:
            self.llm_error = "invalid OpenAI API key (401)"
            self.llm_available = False
        elif "429" in msg or "quota" in low or "insufficient_quota" in low:
            self.llm_error = "OpenAI quota/rate limit (429)"
            self.llm_available = False
        elif "connect" in low or "timeout" in low or "network" in low:
            self.llm_error = "OpenAI unreachable (network)"
        else:
            self.llm_error = msg[:120]

    # ------------------------------------------------------------------ audit
    def _log(self, agent: str, kind: str, model: str, prompt: str,
             response: str, tokens: int = 0, note: str = "") -> None:
        self.call_count += 1
        if not self.log_path:
            return
        rec = (f"\n[{utc_now_iso()}] #{self.call_count} agent={agent} kind={kind} "
               f"model={model} tokens={tokens} note={note}\n"
               f"  PROMPT : {prompt[:800].replace(chr(10), ' ')}\n"
               f"  OUTPUT : {response[:800].replace(chr(10), ' ')}\n")
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(rec)

    @property
    def status(self) -> dict:
        if not self.enabled:
            llm_state = "disabled"
        elif not _OPENAI_LIB:
            llm_state = "library not installed"
        elif not self.has_api_key:
            llm_state = "no API key"
        elif self.llm_error:
            llm_state = self.llm_error
        else:
            llm_state = "ready"
        return {"ai_enabled": self.enabled,
                "embeddings_available": self.embeddings_available,
                "llm_available": self.llm_available,
                "llm_state": llm_state,
                "llm_error": self.llm_error,
                "key_present": self.has_api_key,
                "key_fingerprint": self.key_fingerprint,
                "embedding_model": self.embed_model if self.embeddings_available else None,
                "reasoning_model": self.reasoning_model if self.llm_available else None,
                "llm_calls": self.call_count}

    # -------------------------------------------------------------- embeddings
    def _embedder_model(self):
        if self._embedder is None and self.embeddings_available:
            # CPU + eval mode for reproducible, deterministic embeddings.
            self._embedder = SentenceTransformer(self.embed_model, device="cpu")
            self._embedder.eval()
        return self._embedder

    def _vector(self, text: str) -> Optional[list[float]]:
        if not self.embeddings_available:
            return None
        key = text.strip().upper()
        if key in self._embed_cache:
            return self._embed_cache[key]
        model = self._embedder_model()
        vec = model.encode([key], normalize_embeddings=True,
                           convert_to_numpy=True, show_progress_bar=False)[0]
        # round for cross-run/byte stability of downstream artifacts
        out = [round(float(x), 6) for x in vec]
        self._embed_cache[key] = out
        return out

    def semantic_similarity(self, a: str, b: str) -> Optional[float]:
        """Cosine similarity in [0,1] of two descriptions, or None if disabled."""
        va, vb = self._vector(a), self._vector(b)
        if va is None or vb is None:
            return None
        # vectors are already L2-normalized, so the dot product IS the cosine
        # similarity in [-1, 1]. For description matching, treat unrelated/
        # opposite text (cos <= 0) as 0 and keep positive similarity as-is -
        # a monotonic, continuous map onto [0, 1].
        cos = sum(x * y for x, y in zip(va, vb))
        return round(max(0.0, min(1.0, cos)), 4)

    # --------------------------------------------------------------------- llm
    def _openai(self):
        if self._client is None and self.llm_available:
            # pass the freshly-read key explicitly (don't rely on stale env)
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def chat_json(self, agent: str, system: str, user: str,
                  model: Optional[str] = None) -> Optional[dict]:
        """Deterministic JSON chat completion (temperature 0, fixed seed).

        Returns a parsed dict, or None on any failure (caller falls back).
        """
        if not self.llm_available:
            self._log(agent, "json", model or "-", user, "(llm unavailable - fallback)",
                      note="fallback")
            return None
        client = self._openai()
        mdl = model or self.extraction_model
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=mdl, temperature=0, seed=7,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                text = resp.choices[0].message.content or "{}"
                tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
                data = json.loads(text)
                self._log(agent, "json", mdl, system + "\n" + user, text, tokens,
                          note=f"attempt={attempt}")
                return data
            except Exception as exc:  # network/parse/rate-limit -> retry then fallback
                last_err = str(exc)
                self._note_llm_failure(exc)
                if not self.llm_available:  # invalid key / quota -> don't retry
                    break
                time.sleep(0.5 * (attempt + 1))
        self._log(agent, "json", mdl, user, f"(error: {last_err})", note="error-fallback")
        return None

    def chat_text(self, agent: str, system: str, user: str,
                  model: Optional[str] = None) -> Optional[str]:
        """Free-text completion for natural-language reasoning narratives."""
        if not self.llm_available:
            self._log(agent, "text", model or "-", user, "(llm unavailable - fallback)",
                      note="fallback")
            return None
        client = self._openai()
        mdl = model or self.reasoning_model
        try:
            resp = client.chat.completions.create(
                model=mdl, temperature=0, seed=7,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            text = (resp.choices[0].message.content or "").strip()
            tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
            self._log(agent, "text", mdl, system + "\n" + user, text, tokens)
            return text
        except Exception as exc:
            self._note_llm_failure(exc)
            self._log(agent, "text", mdl, user, f"(error: {exc})", note="error-fallback")
            return None
