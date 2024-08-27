# todo: implement tracing for structured outs. this a v2 feature.
import json
from ell._lstr import _lstr
from functools import cached_property


from pydantic import BaseModel, Field, model_validator
from sqlmodel import Field

from concurrent.futures import ThreadPoolExecutor, as_completed

from typing import Any, Callable, Dict, List, Optional, Type, Union
_lstr_generic = Union[_lstr, str]
InvocableTool = Callable[..., Union["ToolResult", _lstr_generic, List["ContentBlock"], ]]


class ToolResult(BaseModel):
    tool_call_id: _lstr_generic
    result: List["ContentBlock"]

class ToolCall(BaseModel):
    tool : InvocableTool
    tool_call_id : Optional[_lstr_generic] = Field(default=None)
    params : Union[Type[BaseModel], BaseModel]
    def __call__(self, **kwargs):
        assert not kwargs, "Unexpected arguments provided. Calling a tool uses the params provided in the ToolCall."

        # XXX: TODO: MOVE TRACKING CODE TO _TRACK AND OUT OF HERE AND API.
        return self.tool(**self.params.model_dump())

    def call_and_collect_as_message_block(self):
        res = self.tool(**self.params.model_dump(), _tool_call_id=self.tool_call_id)
        return ContentBlock(tool_result=res)

    def call_and_collect_as_message(self):
        return Message(role="user", content=[self.call_and_collect_as_message_block()])


class ContentBlock(BaseModel):
    text: Optional[_lstr_generic] = Field(default=None)
    image: Optional[_lstr_generic] = Field(default=None)
    audio: Optional[_lstr_generic] = Field(default=None)
    tool_call: Optional[ToolCall] = Field(default=None)
    parsed: Optional[Union[Type[BaseModel], BaseModel]] = Field(default=None)
    tool_result: Optional[ToolResult] = Field(default=None)

    @model_validator(mode='after')
    def check_single_non_null(self):
        non_null_fields = [field for field, value in self.__dict__.items() if value is not None]
        if len(non_null_fields) > 1:
            raise ValueError(f"Only one field can be non-null. Found: {', '.join(non_null_fields)}")
        return self

    @property
    def type(self):
        if self.text is not None:
            return "text"
        if self.image is not None:
            return "image"
        if self.audio is not None:
            return "audio"
        if self.tool_call is not None:
            return "tool_call"
        if self.parsed is not None:
            return "parsed"
        if self.tool_result is not None:
            return "tool_result"
        return None

    @classmethod
    def coerce(cls, content: Union[str, ToolCall, ToolResult, BaseModel, "ContentBlock"]) -> ["ContentBlock"]:
        if isinstance(content, ContentBlock):
            return content
        if isinstance(content, str):
            return cls(text=content)
        if isinstance(content, ToolCall):
            return cls(tool_call=content)
        if isinstance(content, ToolResult):
            return cls(tool_result=content)
        if isinstance(content, BaseModel):
            return cls(parsed=content)
        raise ValueError(f"Invalid content type: {type(content)}")

def coerce_content_list(content: Union[str, List[ContentBlock], List[Union[str, ContentBlock, ToolCall, ToolResult, BaseModel]]] = None, **content_block_kwargs) -> List[ContentBlock]:
    if not content:
        content = [ContentBlock(**content_block_kwargs)]

    if not isinstance(content, list):
        content = [content]
    
    return [ContentBlock.model_validate(ContentBlock.coerce(c)) for c in content]

class Message(BaseModel):
    role: str
    content: List[ContentBlock]

    def __init__(self, role, content: Union[str, List[ContentBlock], List[Union[str, ContentBlock, ToolCall, ToolResult, BaseModel]]] = None, **content_block_kwargs):
        content = coerce_content_list(content, **content_block_kwargs)
        super().__init__(content=content, role=role)

    @cached_property
    def text(self) -> str:
        return "\n".join(c.text or f"<{c.type}>" for c in self.content)

    @cached_property
    def text_only(self) -> str:
        return "\n".join(c.text for c in self.content if c.text)

    @cached_property
    def tool_calls(self) -> List[ToolCall]:
        return [c.tool_call for c in self.content if c.tool_call is not None]
    
    @cached_property
    def tool_results(self) -> List[ToolResult]:
        return [c.tool_result for c in self.content if c.tool_result is not None]

    @cached_property
    def parsed_content(self) -> List[BaseModel]:
        return [c.parsed for c in self.content if c.parsed is not None]
    
    def call_tools_and_collect_as_message(self, parallel=False, max_workers=None):
        if parallel:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(c.tool_call.call_and_collect_as_message_block) for c in self.content if c.tool_call]
                content = [future.result() for future in as_completed(futures)]
        else:
            content = [c.tool_call.call_and_collect_as_message_block() for c in self.content if c.tool_call]
        return Message(role="user", content=content)

    def to_openai_message(self) -> Dict[str, Any]:
        message = {
            "role": "tool" if self.tool_results else self.role,
            "content": self.text_only if self.text_only else None
        }

        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": tool_call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.tool.__name__,
                        "arguments": json.dumps(tool_call.params.model_dump())
                    }
                } for tool_call in self.tool_calls
            ]
            message["content"] = None  # Set content to null when there are tool calls

        if self.tool_results:
            message["tool_call_id"] = self.tool_results[0].tool_call_id
            # message["name"] = self.tool_results[0].tool_call_id.split('-')[0]  # Assuming the tool name is the first part of the tool_call_id
            message["content"] = self.tool_results[0].result[0].text
            # Let';s assert no other type of content block in the tool result
            assert len(self.tool_results[0].result) == 1, "Tool result should only have one content block"
            assert self.tool_results[0].result[0].type == "text", "Tool result should only have one text content block"
        return message

# HELPERS 
def system(content: Union[str, List[ContentBlock]]) -> Message:
    """
    Create a system message with the given content.

    Args:
    content (str): The content of the system message.

    Returns:
    Message: A Message object with role set to 'system' and the provided content.
    """
    return Message(role="system", content=content)


def user(content: Union[str, List[ContentBlock]]) -> Message:
    """
    Create a user message with the given content.

    Args:
    content (str): The content of the user message.

    Returns:
    Message: A Message object with role set to 'user' and the provided content.
    """
    return Message(role="user", content=content)


def assistant(content: Union[str, List[ContentBlock]]) -> Message:
    """
    Create an assistant message with the given content.

    Args:
    content (str): The content of the assistant message.

    Returns:
    Message: A Message object with role set to 'assistant' and the provided content.
    """
    return Message(role="assistant", content=content)


# want to enable a use case where the user can actually return a standrd oai chat format
# This is a placehodler will likely come back later for this
LMPParams = Dict[str, Any]
# Well this is disappointing, I wanted to effectively type hint by doign that data sync meta, but eh, at elast we can still reference role or content this way. Probably wil lcan the dict sync meta. TypedDict is the ticket ell oh ell.
MessageOrDict = Union[Message, Dict[str, str]]
# Can support iamge prompts later.
Chat = List[
    Message
]  # [{"role": "system", "content": "prompt"}, {"role": "user", "content": "message"}]
MultiTurnLMP = Callable[..., Chat]
OneTurn = Callable[..., _lstr_generic]
# This is the specific LMP that must accept history as an argument and can take any additional arguments
ChatLMP = Callable[[Chat, Any], Chat]
LMP = Union[OneTurn, MultiTurnLMP, ChatLMP]
InvocableLM = Callable[..., _lstr_generic]