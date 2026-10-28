"""Project validated, source-independent UMIF into existing Vault rows."""

from __future__ import annotations

import json

from everos.umif.models import Message, ToolCall, ToolResult, UmifPackage

from .imports import (
    DEEPSEEK_HARNESS_PARSER_VERSION,
    PARSER_VERSION,
    ParsedImport,
    ParsedMessage,
    ParsedToolEvent,
    _content_hash,
)


def project_for_vault(package: UmifPackage) -> ParsedImport:
    messages = []
    tools = []
    for event in sorted(
        package.events,
        key=lambda item: (str(item.scope.get("session_id")), item.sequence),
    ):
        session = event.scope.get("session_id")
        native_session = next(
            (
                s.payload.get("native_session_id", s.source.get("native_id"))
                for s in package.sessions
                if s.record_id == session
            ),
            session,
        )
        if event.source.get("provider") == "claude":
            native_session = session
        if isinstance(event, Message):
            role = event.payload.get("role")
            if (
                role not in {"user", "assistant"}
                and event.source.get("provider") != "generic"
            ):
                continue
            content = "".join(
                part.get("text", "")
                for part in event.payload.get("content_parts", [])
                if part.get("type") == "text"
            ).strip()
            if not content:
                continue
            ordinal = sum(m.conversation_id == native_session for m in messages)
            native_id = (
                event.record_id
                if event.source.get("provider") == "claude"
                else event.source.get("native_id")
            )
            if event.source.get("provider") == "generic":
                native_id = event.extensions.get("memo_vault_generic", {}).get(
                    "source_message_id"
                )
            messages.append(
                ParsedMessage(
                    native_session,
                    ordinal,
                    role,
                    content,
                    event.timestamp_ms,
                    native_id,
                    _content_hash(content),
                    memory_input=event.routing_hints.get("user_memory") != "deny",
                )
            )
        elif isinstance(event, (ToolCall, ToolResult)):
            call = isinstance(event, ToolCall)
            body = event.payload
            ordinal = sum(t.conversation_id == native_session for t in tools)
            args = body.get("arguments") if call else None
            result_content = body.get("content_parts") if not call else None
            source_event_seqs = (
                tuple(body.get("source_event_seqs") or ()) if not call else ()
            )
            legacy_hash = _content_hash(
                json.dumps(
                    {
                        "event_type": "tool/call" if call else "tool/result",
                        "call_id": body.get("call_id"),
                        "tool_name": body.get("tool_name") if call else None,
                        "arguments": args,
                        "result_content": result_content,
                        "is_error": body.get("is_error") if not call else None,
                        "source_event_seqs": source_event_seqs,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            tools.append(
                ParsedToolEvent(
                    native_session,
                    ordinal,
                    event.record_id
                    if event.source.get("provider") == "claude"
                    else event.source.get("native_id", event.record_id),
                    event.sequence,
                    "tool/call" if call else "tool/result",
                    event.timestamp_ms or 0,
                    body.get("turn") if call else None,
                    body.get("step") if call else None,
                    body.get("call_id"),
                    body.get("tool_name") if call else None,
                    args,
                    result_content,
                    body.get("result_text") if not call else None,
                    body.get("is_error") if not call else None,
                    source_event_seqs,
                    legacy_hash,
                    source_content_hash=event.provenance.get("content_sha256"),
                )
            )
    return ParsedImport(
        messages=tuple(messages),
        conversation_count=len(package.sessions),
        duplicate_message_count=0,
        tool_call_count=sum(t.event_type == "tool/call" for t in tools),
        tool_events=tuple(tools),
        tool_result_count=sum(t.event_type == "tool/result" for t in tools),
        skipped_event_counts=package.import_report.get("skipped_event_counts", {}),
        warnings=tuple(package.import_report.get("warnings", [])),
        parser_version=DEEPSEEK_HARNESS_PARSER_VERSION
        if package.manifest.source_exports[0].get("provider") == "deepseek_harness"
        else PARSER_VERSION,
    )
