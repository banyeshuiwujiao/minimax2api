"""Local patch: images survive the OpenAI -> Anthropic conversion.

Background (2026-10-21): the desktop client talks to this bridge with the
OpenAI Chat Completions dialect, and it can only produce **inline** images --
the harness hands the adapter a durable attachment reference, and the adapter
turns it into a ``data:`` URL.  Two shapes were therefore broken on the way to
the upstream:

1. **A ``data:`` URL was forwarded verbatim as a url source.**  The upstream
   happens to accept that today, but the Anthropic Messages API documents the
   inline form as a base64 source, so the conversion now unpacks ``data:`` URLs
   into the canonical shape instead of depending on that tolerance.

2. **An image inside a tool result was dropped.**  ``read_image`` returns
   ``[text envelope, image block]``; the tool-result branch read the content
   with ``_text_of`` and threw the picture away.  The model then answered
   about a file it had never been shown.  A ``tool_result`` may carry image
   content, so a result with an image now becomes a block list.

These tests pin both behaviours.  They are local additions: upstream's own suite
(``tests/test_bridge.py``) must keep passing unchanged.
"""

from __future__ import annotations

import pytest

from app.model_api import openai_to_anthropic

PNG_DATA = "iVBORw0KGgo="


def _user_message(content):
    return openai_to_anthropic(
        {"model": "minimax-m3.1-flash", "messages": [{"role": "user", "content": content}]},
        "MiniMax-M3.1-Flash",
    )


def test_data_url_becomes_a_base64_image_source() -> None:
    body = _user_message(
        [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_DATA}"}},
        ]
    )
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "看这张图"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_DATA},
    }


def test_http_image_url_stays_a_url_source() -> None:
    body = _user_message(
        [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]
    )
    assert body["messages"][0]["content"][0] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.invalid/a.png"},
    }


def test_malformed_data_url_is_skipped_instead_of_forwarded() -> None:
    body = _user_message(
        [
            {"type": "text", "text": "没有图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
        ]
    )
    assert body["messages"][0]["content"] == [{"type": "text", "text": "没有图"}]


def test_tool_result_keeps_an_image() -> None:
    body = openai_to_anthropic(
        {
            "model": "minimax-m3.1-flash",
            "messages": [
                {"role": "user", "content": "看这张图"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_call", "id": "call_1", "function": {"name": "read_image", "arguments": "{}"}}
                    ],
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "read_image", "arguments": "{}"}}
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": [
                        {"type": "text", "text": "<type>image</type>"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_DATA}"}},
                    ],
                },
            ],
        },
        "MiniMax-M3.1-Flash",
    )
    tool_result = body["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "call_1"
    assert tool_result["content"] == [
        {"type": "text", "text": "<type>image</type>"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_DATA}},
    ]


def test_text_only_tool_result_keeps_the_plain_string_form() -> None:
    body = openai_to_anthropic(
        {
            "model": "minimax-m3.1-flash",
            "messages": [
                {"role": "user", "content": "读一下"},
                {
                    "role": "assistant",
                    "content": [],
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "文件内容"},
            ],
        },
        "MiniMax-M3.1-Flash",
    )
    tool_result = body["messages"][-1]["content"][0]
    assert tool_result["content"] == "文件内容"


@pytest.mark.parametrize(
    "url",
    ["", "data:image/png,notbase64", "data:image/png;base64,"],
)
def test_unusable_image_urls_are_dropped(url: str) -> None:
    body = _user_message([{"type": "image_url", "image_url": {"url": url}}])
    assert body["messages"][0]["content"] == [{"type": "text", "text": ""}]
