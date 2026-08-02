"""Separate a model's chain-of-thought from its answer, while it streams.

Reasoning reaches us over the OpenAI-compatible wire in two shapes:

* a **dedicated delta field** — ``reasoning_content`` (DeepSeek, llama.cpp with
  ``--reasoning-format deepseek``, Ollama's thinking models) or ``reasoning``
  (OpenRouter). Already separated by the endpoint; we only have to pick it up.
* **inline ``<think>…</think>``** inside ``content`` — Qwen3, DeepSeek-R1 and
  friends served raw. The tags are just text, so they arrive cut in half by
  chunk boundaries and the scan has to be stateful.

Keeping the two apart matters beyond cosmetics: the answer is what gets stored
in ``conversation[]``, re-sent to the model next turn, handed to the channels
and *spoken* by a voice satellite. Thinking belongs to none of those.
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"

# Delta fields carrying reasoning the endpoint already split out for us.
_DELTA_FIELDS = ("reasoning_content", "reasoning")


def _hold(text: str, tag: str) -> int:
    """Length of the trailing slice of `text` that may be a cut-off `tag`.

    A chunk can end mid-tag (``…blah <thi``), so that tail is kept back rather
    than streamed out as answer text — otherwise the opening tag would reach
    the user split across two renders and never match."""
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:n]):
            return n
    return 0


class ReasoningSplitter:
    """Feed it raw stream deltas, get ``(answer, reasoning, reclassify)`` back.

    One instance per LLM call (a turn with tool calls makes several).

    ``reclassify`` is the awkward case: chat templates for Qwen3/DeepSeek
    **prefill** ``<think>`` at the end of the prompt, so the model starts
    *inside* the block and only ever emits the closing tag. Everything before
    that ``</think>`` was thinking — including whatever we already handed out
    as answer. The flag tells the caller to take it back (the SSE stream has a
    ``clear_tokens`` event for exactly this).
    """

    def __init__(self) -> None:
        self.inside = False
        self._buf = ""          # text held back: possibly a cut-off tag
        self._seen_tag = False  # a tag was seen; no prefilled-open guess any more
        self._answered = False  # answer text already handed to the caller

    def feed(self, delta: dict) -> tuple[str, str, bool]:
        reasoning = ""
        for field in _DELTA_FIELDS:
            part = delta.get(field)
            if isinstance(part, str) and part:
                reasoning += part
        content = delta.get("content")
        answer, inline, reclassify = (
            self._scan(content) if isinstance(content, str) and content
            else ("", "", False)
        )
        if answer:
            self._answered = True
        return answer, reasoning + inline, reclassify

    def finish(self) -> tuple[str, str]:
        """Flush what is still held back, at end of stream.

        An unterminated block (the model hit its token cap mid-thought) stays
        reasoning: it is not an answer, and promoting it would put a stray
        ``<think>`` into the conversation and into a satellite's mouth."""
        rest, self._buf = self._buf, ""
        if self.inside:
            return "", rest
        if rest:
            self._answered = True
        return rest, ""

    # ------------------------------------------------------------------ scan
    def _scan(self, text: str) -> tuple[str, str, bool]:
        self._buf += text
        answer: list[str] = []
        reasoning: list[str] = []
        reclassify = False

        while self._buf:
            if self.inside:
                i = self._buf.find(_CLOSE)
                if i < 0:
                    keep = _hold(self._buf, _CLOSE)
                    reasoning.append(self._buf[:len(self._buf) - keep])
                    self._buf = self._buf[len(self._buf) - keep:]
                    break
                reasoning.append(self._buf[:i])
                self._buf = self._buf[i + len(_CLOSE):]
                self.inside = False
                continue

            i = self._buf.find(_OPEN)
            # A closing tag with no opening one before it: the template opened
            # the block for the model, so everything up to here was thinking.
            j = -1 if self._seen_tag else self._buf.find(_CLOSE)
            if j >= 0 and (i < 0 or j < i):
                reasoning.append(self._buf[:j])
                self._buf = self._buf[j + len(_CLOSE):]
                self._seen_tag = True
                reclassify = reclassify or self._answered
                continue
            if i >= 0:
                answer.append(self._buf[:i])
                self._buf = self._buf[i + len(_OPEN):]
                self.inside = True
                self._seen_tag = True
                continue

            keep = _hold(self._buf, _OPEN)
            if not self._seen_tag:
                keep = max(keep, _hold(self._buf, _CLOSE))
            answer.append(self._buf[:len(self._buf) - keep])
            self._buf = self._buf[len(self._buf) - keep:]
            break

        return "".join(answer), "".join(reasoning), reclassify


def strip_reasoning(text: str) -> str:
    """The answer only, for text that arrived in one piece (no streaming)."""
    if not text:
        return text
    splitter = ReasoningSplitter()
    answer, _, _ = splitter.feed({"content": text})
    tail, _ = splitter.finish()
    return (answer + tail).strip()
