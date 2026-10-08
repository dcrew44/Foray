from django.apps import AppConfig


class LlmConfig(AppConfig):
    name = "apps.llm"
    label = "llm"
    verbose_name = "LLM layer"

    def ready(self) -> None:
        import pydantic_ai

        # pydantic-ai prints a first-run banner on a terminal; the commands own their output
        # (research/llm-library.md §12).
        pydantic_ai.BANNER_ENABLED = False
