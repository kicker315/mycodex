from mint_codex.core.providers import default_provider_configs
from mint_codex.models.session import Thread


def test_default_providers_use_separate_codex_homes_and_secret_indirection(tmp_path):
    configs = default_provider_configs(home=tmp_path)

    assert configs["openai"].codex_home == tmp_path / ".codex"
    assert configs["deepseek"].codex_home == tmp_path / ".codex-deepseek"
    assert configs["openai"].codex_home != configs["deepseek"].codex_home
    assert configs["deepseek"].env_key == "DEEPSEEK_API_KEY"
    assert configs["deepseek"].wire_api == "responses"
    assert configs["deepseek"].model == "deepseek-v4-flash"
    assert all(
        "api_key" not in argument.lower() or "env_key" in argument
        for argument in configs["deepseek"].config_overrides
    )


def test_thread_carries_provider_and_model_metadata():
    thread = Thread("thread-1", provider="deepseek", model="deepseek-v4-flash")

    assert thread.provider == "deepseek"
    assert thread.model == "deepseek-v4-flash"
