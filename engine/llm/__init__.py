"""Provider-agnostic LLM client layer (Stage 4).

``ai_extract`` and the cover-letter / tailoring stages talk to an
:class:`~engine.llm.client.LLMClient` built by :func:`~engine.llm.client.make_client`
rather than to the Anthropic SDK directly. Anthropic is the only wired
provider today; OpenAI/Gemini are explicit NotImplemented stubs so adding
one is a single new class + factory branch.
"""
from .client import (  # noqa: F401
    AnthropicClient,
    LLMClient,
    LLMError,
    UnsupportedProviderError,
    make_client,
)
