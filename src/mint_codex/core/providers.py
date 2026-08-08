from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration owned by one isolated Codex app-server process."""

    id: str
    display_name: str
    codex_home: Path
    model: str | None = None
    env_key: str | None = None
    base_url: str | None = None
    wire_api: str = "responses"
    requires_openai_auth: bool = True
    config_overrides: tuple[str, ...] = ()

    @property
    def is_custom(self) -> bool:
        return bool(self.config_overrides)


def default_provider_configs(home: Path | None = None) -> dict[str, ProviderConfig]:
    """Return the two MVP providers without reading or changing either config.toml."""

    root = (home or Path.home()).expanduser().resolve()
    deepseek_base_url = "https://api.deepseek.com/v1"
    deepseek = ProviderConfig(
        id="deepseek",
        display_name="DeepSeek",
        codex_home=root / ".codex-deepseek",
        model="deepseek-v4-flash",
        env_key="DEEPSEEK_API_KEY",
        base_url=deepseek_base_url,
        wire_api="responses",
        requires_openai_auth=False,
        config_overrides=(
            'model_provider="deepseek"',
            'model="deepseek-v4-flash"',
            (
                'model_providers.deepseek={name="DeepSeek",'
                f'base_url="{deepseek_base_url}",'
                'env_key="DEEPSEEK_API_KEY",wire_api="responses",'
                'requires_openai_auth=false}'
            ),
        ),
    )
    return {
        "openai": ProviderConfig(
            id="openai",
            display_name="OpenAI",
            codex_home=root / ".codex",
            wire_api="responses",
            requires_openai_auth=True,
        ),
        "deepseek": deepseek,
    }
