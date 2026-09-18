"""Provider registry and configuration resolution.

**Standard library only** — `resolve/SubTranslator.py` imports this to report which engine
ran, and it runs under Resolve's embedded Python which cannot see the venv. Same constraint
as `langtags.py` and `resolve_drt.py`. That is also why the `.env` reader here is hand-rolled
rather than `python-dotenv`.

Resolution order, first match wins:

    explicit argument  ->  SUBTRANS_* env  ->  project .env  ->  built-in default (mistral)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PROVIDERS", "ProviderSpec", "Settings", "ConfigError", "credential_for",
    "resolve", "load_dotenv", "write_env", "configured_providers", "DEFAULT_PROVIDER",
]

DEFAULT_PROVIDER = "mistral"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Configuration is missing or unusable. The message is shown to the operator."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    kind: str  # "anthropic" | "openai"  — which adapter drives it
    default_model: str
    env_key: str | None = None  # None => no credential needed (local)
    # Other names accepted for the same credential, so an existing .env keeps working.
    env_aliases: tuple[str, ...] = ()
    base_url: str | None = None  # None => the SDK's own default
    signup_url: str | None = None
    free: bool = False
    strict_schema: bool = True  # provider honours a strict json_schema response format
    notes: str = ""


# Model ids are deliberately "-latest" aliases where the provider offers them: pinned
# snapshots get deprecated (Mistral retired magistral-small-2509 mid-2026).
PROVIDERS: dict[str, ProviderSpec] = {
    "mistral": ProviderSpec(
        name="mistral",
        label="Mistral (free tier)",
        kind="openai",
        default_model="mistral-large-latest",
        env_key="MISTRAL_API_KEY",
        env_aliases=("MISTRAL_API", "MISTRAL_KEY"),
        base_url="https://api.mistral.ai/v1",
        signup_url="https://console.mistral.ai/",
        free=True,
        notes="No credit card. ~1B tokens/month on the Experiment tier.",
    ),
    "claude": ProviderSpec(
        name="claude",
        label="Claude (best quality)",
        kind="anthropic",
        default_model="claude-opus-4-8",
        env_key="ANTHROPIC_API_KEY",
        env_aliases=("ANTHROPIC_KEY",),
        signup_url="https://console.anthropic.com/",
        notes="Paid. Highest translation quality in testing.",
    ),
    "groq": ProviderSpec(
        name="groq",
        label="Groq",
        kind="openai",
        default_model="llama-3.3-70b-versatile",
        env_key="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        signup_url="https://console.groq.com/",
        free=True,
        strict_schema=False,
        notes="Free tier, very fast. Verify the model id with --list-models.",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        label="OpenRouter",
        kind="openai",
        default_model="mistralai/mistral-large",
        env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        signup_url="https://openrouter.ai/keys",
        strict_schema=False,
        notes="Routes to many models, some free. Set --model explicitly.",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        label="Ollama (local)",
        kind="openai",
        default_model="gemma3:12b",
        env_key=None,
        base_url="http://localhost:11434/v1",
        strict_schema=False,
        notes="Local, no key. Slow without a supported GPU.",
    ),
    "custom": ProviderSpec(
        name="custom",
        label="Custom OpenAI-compatible endpoint",
        kind="openai",
        default_model="",
        env_key="SUBTRANS_API_KEY",
        env_aliases=("OPENAI_API_KEY",),
        base_url=None,  # from SUBTRANS_BASE_URL
        strict_schema=False,
        notes="Set SUBTRANS_BASE_URL and SUBTRANS_MODEL.",
    ),
}


@dataclass(frozen=True)
class Settings:
    spec: ProviderSpec
    model: str
    api_key: str | None
    base_url: str | None

    @property
    def provider(self) -> str:
        return self.spec.name

    @property
    def label(self) -> str:
        return self.spec.label


def load_dotenv(path: str | os.PathLike | None = None, override: bool = False) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE, `export` prefix, #comments, optional quotes.

    Values are pushed into os.environ (never overwriting a real env var unless asked), and
    also returned so a caller can inspect them without touching the environment.
    """
    path = Path(path) if path else PROJECT_ROOT / ".env"
    found: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return found
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        found[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return found


def write_env(provider: str, key: str = "", path: str | os.PathLike | None = None) -> str:
    """Upsert SUBTRANS_PROVIDER, and the provider's key, into .env. Returns a summary line.

    Both installers (install.sh, install.ps1) call this rather than editing .env themselves,
    so neither has to carry its own copy of the provider -> env-var-name mapping.
    """
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise ConfigError(
            f"unknown provider {provider!r}. Available: {', '.join(sorted(PROVIDERS))}"
        )
    key_var = PROVIDERS[provider].env_key
    target = Path(path) if path else PROJECT_ROOT / ".env"
    text = target.read_text(encoding="utf-8") if target.exists() else ""

    def upsert(text: str, name: str | None, value: str) -> str:
        if not name:
            return text
        # Matches the live line and a commented-out template line alike, so .env.example
        # placeholders get filled in rather than shadowed by an appended duplicate.
        pattern = re.compile(rf"^#?\s*{re.escape(name)}=.*$", re.M)
        line = f"{name}={value}"
        return pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip() + f"\n{line}\n"

    text = upsert(text, "SUBTRANS_PROVIDER", provider)
    if key:
        text = upsert(text, key_var, key)
    target.write_text(text, encoding="utf-8")
    return f"{target} set to provider={provider}" + (f", {key_var} written" if key else "")


def credential_for(spec: ProviderSpec) -> str | None:
    """First non-empty value among the spec's primary key name and its aliases."""
    for name in (spec.env_key, *spec.env_aliases):
        if name and os.getenv(name):
            return os.environ[name]
    return None


def configured_providers() -> list[ProviderSpec]:
    """Providers that could actually run right now — key present, or none needed."""
    return [s for s in PROVIDERS.values() if s.env_key is None or credential_for(s)]


def resolve(provider: str | None = None, model: str | None = None) -> Settings:
    """Work out which provider and model to use, and fetch the credential."""
    load_dotenv()

    name = (provider or os.getenv("SUBTRANS_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise ConfigError(
            f"unknown provider {name!r}. Available: {', '.join(sorted(PROVIDERS))}"
        )
    spec = PROVIDERS[name]

    chosen_model = model or os.getenv("SUBTRANS_MODEL") or spec.default_model
    if not chosen_model:
        raise ConfigError(
            f"provider {name!r} has no default model — pass --model or set SUBTRANS_MODEL"
        )

    base_url = spec.base_url or os.getenv("SUBTRANS_BASE_URL")
    if spec.kind == "openai" and not base_url:
        raise ConfigError(
            f"provider {name!r} needs a base URL — set SUBTRANS_BASE_URL"
        )

    api_key = credential_for(spec) if spec.env_key else None
    if spec.env_key and not api_key:
        raise ConfigError(_missing_key_message(spec))

    return Settings(spec=spec, model=chosen_model, api_key=api_key, base_url=base_url)


def _missing_key_message(spec: ProviderSpec) -> str:
    """Actionable, not a stack trace — this is what a producer will actually see."""
    lines = [
        f"No API key for {spec.label}.",
        "",
        f"Set {spec.env_key} in {PROJECT_ROOT / '.env'}",
    ]
    if spec.env_aliases:
        lines.append(f"(also accepted: {', '.join(spec.env_aliases)})")
    lines += [
    ]
    if spec.signup_url:
        cost = "free, no credit card" if spec.free else "paid account"
        lines.append(f"Get a key at {spec.signup_url}  ({cost})")
    ready = [s for s in configured_providers() if s.name != spec.name]
    if ready:
        lines += ["", "Already configured here: " + ", ".join(s.name for s in ready)]
    return "\n".join(lines)
