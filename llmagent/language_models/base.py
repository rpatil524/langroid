import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import aiohttp
from pydantic import BaseModel, BaseSettings

from llmagent.cachedb.redis_cachedb import RedisCacheConfig
from llmagent.mytypes import Document
from llmagent.prompts.dialog import collate_chat_history
from llmagent.prompts.templates import (
    EXTRACTION_PROMPT_GPT4,
    SUMMARY_ANSWER_PROMPT_GPT4,
)
from llmagent.utils.configuration import settings
from llmagent.utils.output.printing import show_if_debug


class LLMConfig(BaseSettings):
    type: str = "openai"
    timeout: int = 20  # timeout for API requests
    chat_model: Optional[str] = None
    completion_model: Optional[str] = None
    context_length: Optional[Dict[str, int]] = None
    max_output_tokens: int = 1024  # generate at most this many tokens
    # if input length + max_output_tokens > context length of model,
    # we will try shortening requested output
    min_output_tokens: int = 64
    use_chat_for_completion: bool = True  # use chat model for completion?
    stream: bool = False  # stream output from API?
    cache_config: RedisCacheConfig = RedisCacheConfig(
        hostname="redis-11524.c251.east-us-mz.azure.cloud.redislabs.com",
        port=11524,
    )


class LLMFunctionCall(BaseModel):
    """
    Structure of LLM response indicate it "wants" to call a function.
    Modeled after OpenAI spec for `function_call` field in ChatCompletion API.
    """

    name: str  # name of function to call
    to: str = ""  # intended recipient
    arguments: Optional[Dict[str, Any]] = None


class LLMResponse(BaseModel):
    message: str
    function_call: Optional[LLMFunctionCall] = None
    usage: int
    cached: bool = False


class LLMFunctionSpec(BaseModel):
    """
    Description of a function available for the LLM to use.
    To be used when calling the LLM `chat()` method with the `functions` parameter.
    Modeled after OpenAI spec for `functions` fields in ChatCompletion API.
    """

    name: str
    description: str
    parameters: Dict[str, Any]


class Role(str, Enum):
    USER = "user"
    SYSTEM = "system"
    ASSISTANT = "assistant"
    FUNCTION = "function"


class LLMMessage(BaseModel):
    role: Role
    name: str = "xyz"
    content: str

    def __str__(self) -> str:
        return f"{self.role} ({self.name}): {self.content}"


# Define an abstract base class for language models
class LanguageModel(ABC):
    """
    Abstract base class for language models.
    """

    def __init__(self, config: LLMConfig):
        self.config = config

    @staticmethod
    def create(config: Optional[LLMConfig]) -> Optional[Type["LanguageModel"]]:
        """
        Create a language model.
        Args:
            config: configuration for language model
        Returns: instance of language model
        """
        from llmagent.language_models.openai_gpt import OpenAIGPT

        if config is None or config.type is None:
            return None
        cls = dict(
            openai=OpenAIGPT,
        ).get(config.type, OpenAIGPT)
        return cls(config)  # type: ignore

    @abstractmethod
    def set_stream(self, stream: bool) -> bool:
        """Enable or disable streaming output from API.
        Return previous value of stream."""
        pass

    @abstractmethod
    def get_stream(self) -> bool:
        """Get streaming status"""
        pass

    @abstractmethod
    def generate(self, prompt: str, max_tokens: int) -> LLMResponse:
        pass

    @abstractmethod
    async def agenerate(self, prompt: str, max_tokens: int) -> LLMResponse:
        pass

    @abstractmethod
    def chat(
        self,
        messages: Union[str, List[LLMMessage]],
        max_tokens: int,
        functions: Optional[List[LLMFunctionSpec]] = None,
        function_call: str | Dict[str, str] = "auto",
    ) -> LLMResponse:
        pass

    def __call__(self, prompt: str, max_tokens: int) -> LLMResponse:
        return self.generate(prompt, max_tokens)

    def chat_context_length(self) -> int:
        if self.config.chat_model is None:
            raise ValueError("No chat model specified")
        if self.config.context_length is None:
            raise ValueError("No context length  specified")
        return self.config.context_length[self.config.chat_model]

    def completion_context_length(self) -> int:
        if self.config.completion_model is None:
            raise ValueError("No completion model specified")
        if self.config.context_length is None:
            raise ValueError("No context length  specified")
        return self.config.context_length[self.config.completion_model]

    def followup_to_standalone(
        self, chat_history: List[Tuple[str, str]], question: str
    ) -> str:
        """
        Given a chat history and a question, convert it to a standalone question.
        Args:
            chat_history: list of tuples of (question, answer)
            query: follow-up question

        Returns: standalone version of the question
        """
        history = collate_chat_history(chat_history)

        prompt = f"""
        Given the conversationn below, and a follow-up question, rephrase the follow-up 
        question as a standalone question.
        
        Chat history: {history}
        Follow-up question: {question} 
        """.strip()
        show_if_debug(prompt, "FOLLOWUP->STANDALONE-PROMPT= ")
        standalone = self.generate(prompt=prompt, max_tokens=1024).message.strip()
        show_if_debug(prompt, "FOLLOWUP->STANDALONE-RESPONSE= ")
        return standalone

    async def get_verbatim_extract_async(self, question: str, passage: Document) -> str:
        """
        Asynchronously, get verbatim extract from passage
        that is relevant to a question.
        Asynch allows parallel calls to the LLM API.
        """
        async with aiohttp.ClientSession():
            templatized_prompt = EXTRACTION_PROMPT_GPT4
            final_prompt = templatized_prompt.format(
                question=question, content=passage.content
            )
            show_if_debug(final_prompt, "EXTRACT-PROMPT= ")
            final_extract = await self.agenerate(prompt=final_prompt, max_tokens=1024)
            show_if_debug(final_extract.message.strip(), "EXTRACT-RESPONSE= ")
        return final_extract.message.strip()

    async def _get_verbatim_extracts(
        self,
        question: str,
        passages: List[Document],
    ) -> List[Document]:
        async with aiohttp.ClientSession():
            verbatim_extracts = await asyncio.gather(
                *(self.get_verbatim_extract_async(question, P) for P in passages)
            )
        metadatas = [P.metadata for P in passages]
        # return with metadata so we can use it downstream, e.g. to cite sources
        return [
            Document(content=e, metadata=m)
            for e, m in zip(verbatim_extracts, metadatas)
        ]

    def get_verbatim_extracts(
        self, question: str, passages: List[Document]
    ) -> List[Document]:
        """
        From each passage, extract verbatim text that is relevant to a question,
        using concurrent API calls to the LLM.
        Args:
            question: question to be answered
            passages: list of passages from which to extract relevant verbatim text
            LLM: LanguageModel to use for generating the prompt and extract
        Returns:
            list of verbatim extracts from passages that are relevant to question
        """
        docs = asyncio.run(self._get_verbatim_extracts(question, passages))
        return docs

    def get_summary_answer(self, question: str, passages: List[Document]) -> Document:
        """
        Given a question and a list of (possibly) doc snippets,
        generate an answer if possible
        Args:
            question: question to answer
            passages: list of `Document` objects each containing a possibly relevant
                snippet, and metadata
        Returns:
            a `Document` object containing the answer,
            and metadata containing source citations

        """

        # Define an auxiliary function to transform the list of
        # passages into a single string
        def stringify_passages(passages: List[Document]) -> str:
            return "\n".join(
                [
                    f"""
                Extract: {p.content}
                Source: {p.metadata.source}
                """
                    for p in passages
                ]
            )

        passages_str = stringify_passages(passages)
        # Substitute Q and P into the templatized prompt

        final_prompt = SUMMARY_ANSWER_PROMPT_GPT4.format(
            question=f"Question:{question}", extracts=passages_str
        )
        show_if_debug(final_prompt, "SUMMARIZE_PROMPT= ")
        # Generate the final verbatim extract based on the final prompt
        llm_response = self.generate(prompt=final_prompt, max_tokens=1024)
        final_answer = llm_response.message.strip()
        show_if_debug(final_answer, "SUMMARIZE_RESPONSE= ")
        parts = final_answer.split("SOURCE:", maxsplit=1)
        if len(parts) > 1:
            content = parts[0].strip()
            sources = parts[1].strip()
        else:
            content = final_answer
            sources = ""
        return Document(
            content=content,
            metadata={"source": "SOURCE: " + sources, "cached": llm_response.cached},
        )


class StreamingIfAllowed:
    """Context to temporarily enable or disable streaming, if allowed globally via
    `settings.stream`"""

    def __init__(self, llm: LanguageModel, stream: bool = True):
        self.llm = llm
        self.stream = stream

    def __enter__(self) -> None:
        self.old_stream = self.llm.set_stream(settings.stream and self.stream)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.llm.set_stream(self.old_stream)
