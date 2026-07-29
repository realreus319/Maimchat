from __future__ import annotations

import re
import sys
from typing import Any, Mapping, Sequence, TextIO

from ..bootstrap import getAllowedChannels, getQuestionPreviewFormat

_ASK_USER_QUESTION_CHANNELS_DISABLED_MESSAGE = (
    "AskUserQuestion is disabled while channels mode is active"
)
_HTML_DOCUMENT_RE = re.compile(r"<\s*(html|body|!doctype)\b", re.IGNORECASE)
_HTML_SCRIPT_STYLE_RE = re.compile(r"<\s*(script|style)\b", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[a-z][^>]*>", re.IGNORECASE)


def ask_user_question(
    raw_input: Mapping[str, Any],
    *,
    normalized_questions: Sequence[Mapping[str, Any]] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> dict[str, Any]:
    source_questions = (
        tuple(dict(question) for question in normalized_questions)
        if normalized_questions is not None
        else _normalize_questions(raw_input)
    )
    answers: dict[str, Any] = {}
    asked_questions: list[dict[str, Any]] = []
    stdin = input_stream or sys.stdin
    stdout = output_stream or sys.stdout

    for index, question in enumerate(source_questions, start=1):
        answer = _ask_single_question(question, index=index, stdin=stdin, stdout=stdout)
        asked_question = {
            "id": question["id"],
            "question": question["question"],
            "header": question["header"],
            "selected": answer,
        }
        for optional_key in ("preview", "annotations", "metadata"):
            if optional_key in question:
                asked_question[optional_key] = question[optional_key]
        asked_questions.append(asked_question)
        answers[question["id"]] = answer

    if len(asked_questions) == 1:
        only = asked_questions[0]
        selected = only["selected"]
        if isinstance(selected, list):
            selected = selected[0] if selected else ""
        return {
            "selected": selected,
            "question": only["question"],
            "questions": asked_questions,
            "answers": answers,
        }

    return {
        "questions": asked_questions,
        "answers": answers,
    }


def is_ask_user_question_enabled() -> bool:
    return len(getAllowedChannels()) == 0


def ask_user_question_disabled_message() -> str:
    return _ASK_USER_QUESTION_CHANNELS_DISABLED_MESSAGE


def normalize_ask_user_questions(
    raw_input: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    return _normalize_questions(raw_input)


def _normalize_questions(raw_input: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    if "questions" in raw_input:
        value = raw_input["questions"]
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("Expected questions to be an array")
        normalized = tuple(_normalize_question(question, index) for index, question in enumerate(value, start=1))
    else:
        normalized = (_normalize_question(raw_input, 1),)

    if not 1 <= len(normalized) <= 4:
        raise ValueError("AskUserQuestion supports between 1 and 4 questions")
    return normalized


def _normalize_question(question: Any, index: int) -> dict[str, Any]:
    if not isinstance(question, Mapping):
        raise ValueError("Expected each question to be an object")

    prompt = question.get("question")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Expected non-empty string for question")

    raw_options = question.get("options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        raise ValueError("Expected options to be an array")
    if not 2 <= len(raw_options) <= 4:
        raise ValueError("Each question must provide between 2 and 4 options")

    options = tuple(_normalize_option(option, option_index) for option_index, option in enumerate(raw_options, start=1))
    header = question.get("header")
    if not isinstance(header, str) or not header.strip():
        header = f"Q{index}"
    header = header.strip()
    if len(header) > 12:
        raise ValueError("Question header must be 12 characters or fewer")

    question_id = question.get("id")
    if not isinstance(question_id, str) or not question_id.strip():
        question_id = f"question_{index}"

    normalized = {
        "id": question_id,
        "header": header,
        "question": prompt.strip(),
        "options": options,
        "multiSelect": bool(question.get("multiSelect")),
    }
    preview = question.get("preview")
    if preview is not None:
        if not isinstance(preview, str):
            raise ValueError("Expected string for preview")
        preview_error = _validate_html_preview(preview)
        if preview_error is not None:
            raise ValueError(preview_error)
        normalized["preview"] = preview
    annotations = question.get("annotations")
    if annotations is not None:
        if not isinstance(annotations, Mapping):
            raise ValueError("Expected annotations to be an object")
        normalized["annotations"] = dict(annotations)
    metadata = question.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise ValueError("Expected metadata to be an object")
        normalized["metadata"] = dict(metadata)
    return normalized


def _validate_html_preview(preview: str | None) -> str | None:
    if preview is None or getQuestionPreviewFormat() != "html":
        return None
    if _HTML_DOCUMENT_RE.search(preview):
        return (
            "preview must be an HTML fragment, not a full document "
            "(no <html>, <body>, or <!DOCTYPE>)"
        )
    if _HTML_SCRIPT_STYLE_RE.search(preview):
        return (
            "preview must not contain <script> or <style> tags. "
            "Use inline styles via the style attribute if needed."
        )
    if not _HTML_TAG_RE.search(preview):
        return (
            'preview must contain HTML (previewFormat is set to "html"). '
            "Wrap content in a tag like <div> or <pre>."
        )
    return None


def _normalize_option(option: Any, index: int) -> dict[str, str]:
    if not isinstance(option, Mapping):
        raise ValueError("Expected each option to be an object")

    label = option.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("Expected non-empty string for option label")

    key = option.get("id") or option.get("key") or label
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Expected non-empty string for option key")

    description = option.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("Expected string for option description")

    return {
        "key": key.strip(),
        "label": label.strip(),
        "description": description.strip() if isinstance(description, str) else "",
        "ordinal": str(index),
    }


def _ask_single_question(
    question: Mapping[str, Any],
    *,
    index: int,
    stdin: TextIO,
    stdout: TextIO,
) -> str | list[str]:
    options = list(question["options"])
    options.append({"key": "__other__", "label": "Other", "description": "", "ordinal": str(len(options) + 1)})

    if not _is_interactive(stdin, stdout):
        selected = options[0]
        return [selected["label"]] if question["multiSelect"] else selected["label"]

    stdout.write(f"? [{question['header']}] {question['question']}\n")
    for option in options:
        line = f"{option['ordinal']}. {option['label']}"
        if option["description"]:
            line += f" - {option['description']}"
        stdout.write(line + "\n")
    stdout.write("> ")
    stdout.flush()

    raw_answer = stdin.readline()
    if raw_answer == "":
        selected = options[0]
        return [selected["label"]] if question["multiSelect"] else selected["label"]

    parts = [part.strip() for part in raw_answer.split(",") if part.strip()]
    if not parts:
        selected = options[0]
        return [selected["label"]] if question["multiSelect"] else selected["label"]

    resolved = [_resolve_choice(part, options, stdin=stdin, stdout=stdout) for part in parts]
    if question["multiSelect"]:
        return resolved
    return resolved[0]


def _resolve_choice(
    value: str,
    options: Sequence[Mapping[str, str]],
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> str:
    lowered = value.lower()
    for option in options:
        if lowered in {option["ordinal"].lower(), option["key"].lower(), option["label"].lower()}:
            if option["key"] != "__other__":
                return option["label"]
            stdout.write("Other: ")
            stdout.flush()
            other_value = stdin.readline().strip()
            return other_value or option["label"]
    raise ValueError(f"Invalid option selection: {value}")


def _is_interactive(stdin: TextIO, stdout: TextIO) -> bool:
    stdin_tty = getattr(stdin, "isatty", None)
    stdout_tty = getattr(stdout, "isatty", None)
    return bool(callable(stdin_tty) and stdin_tty() and callable(stdout_tty) and stdout_tty())


__all__ = [
    "ask_user_question",
    "ask_user_question_disabled_message",
    "is_ask_user_question_enabled",
    "normalize_ask_user_questions",
]
