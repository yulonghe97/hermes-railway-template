#!/usr/bin/env python3
"""Idempotently apply vendor patches to the installed Hermes.

Applied during Docker image build (see Dockerfile) so the patches are
baked into the runtime layer — live from the first second after a
redeploy, no runtime race. Each patch is guarded by a marker check,
so this script is safe to re-run.

Current patches:
- **slack-strict-mention** (Hermes PR #12258 — still open upstream as
  of this writing) — adds a `slack.strict_mention: true` config key.
  When enabled, channel threads require an explicit `@mention` on every
  message to trigger the bot. Without it, the bot auto-replies to every
  message under a thread where it (or a participant) was mentioned,
  which is noisy for posts like daily standups that humans chat under.
- **send-message-edit-action** — adds `action="edit"` to the
  `send_message` tool schema plus a Slack edit path that routes to
  `chat.update` under the hood. Without it the agent can send Slack
  messages but not edit them, so corrections have to be posted as
  fresh messages that clutter the thread.

Remove a patch from this file once it lands in an upstream Hermes
release that is resolved by this template's `HERMES_GIT_REF`.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Callable

HERMES = pathlib.Path(os.environ.get("HERMES_SRC_DIR", "/opt/hermes-agent"))


def _apply(name: str, fn: Callable[[], bool]) -> None:
    try:
        changed = fn()
    except Exception as e:
        print(f"[patch:{name}] FAILED: {e}", file=sys.stderr)
        raise
    tag = "applied" if changed else "already present"
    print(f"[patch:{name}] {tag}")


def slack_strict_mention() -> bool:
    """Hermes PR #12258 — slack.strict_mention config.

    Adds:
    - gateway/config.py: yaml-key → env-var bridge for `SLACK_STRICT_MENTION`
    - gateway/platforms/slack.py: gate that returns early when
      `strict_mention=true` and the message isn't `@`-mentioned,
      disabling the `_bot_message_ts` and `_mentioned_threads`
      auto-triggers.
    """
    cfg_path = HERMES / "gateway" / "config.py"
    slk_path = HERMES / "gateway" / "platforms" / "slack.py"

    c = cfg_path.read_text()
    s = slk_path.read_text()

    already_c = "SLACK_STRICT_MENTION" in c
    already_s = "_slack_strict_mention" in s
    if already_c and already_s:
        return False

    # ── config.py: bridge yaml key → env var ─────────────────────
    if not already_c:
        old_c = (
            '                if "allow_bots" in slack_cfg and not os.getenv("SLACK_ALLOW_BOTS"):\n'
        )
        new_c = (
            '                if "strict_mention" in slack_cfg and not os.getenv("SLACK_STRICT_MENTION"):\n'
            '                    os.environ["SLACK_STRICT_MENTION"] = str(slack_cfg["strict_mention"]).lower()\n'
            '                if "allow_bots" in slack_cfg and not os.getenv("SLACK_ALLOW_BOTS"):\n'
        )
        if c.count(old_c) != 1:
            raise RuntimeError(
                f"config.py anchor not found or not unique (count={c.count(old_c)}). "
                f"Hermes upstream at {cfg_path} may have moved — inspect and update."
            )
        cfg_path.write_text(c.replace(old_c, new_c))

    # ── slack.py: the gate + the helper ──────────────────────────
    if not already_s:
        gate_old = (
            "            elif not self._slack_require_mention():\n"
            "                pass  # Mention requirement disabled globally for Slack\n"
            "            elif not is_mentioned:\n"
        )
        gate_new = (
            "            elif not self._slack_require_mention():\n"
            "                pass  # Mention requirement disabled globally for Slack\n"
            "            elif self._slack_strict_mention() and not is_mentioned:\n"
            "                return  # Strict mode: ignore until @-mentioned again\n"
            "            elif not is_mentioned:\n"
        )
        if s.count(gate_old) != 1:
            raise RuntimeError(
                f"slack.py gate anchor not found (count={s.count(gate_old)}) in {slk_path}."
            )
        s = s.replace(gate_old, gate_new)

        helper_old = (
            '        return os.getenv("SLACK_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no", "off")\n'
            "\n"
            "    def _slack_free_response_channels(self) -> set:\n"
        )
        helper_new = (
            '        return os.getenv("SLACK_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no", "off")\n'
            "\n"
            "    def _slack_strict_mention(self) -> bool:\n"
            '        """When true, channel threads require an explicit @-mention on every\n'
            "        message. Disables all auto-triggers (mentioned-thread memory,\n"
            "        bot-message follow-up, session-presence). Defaults to False.\n"
            '        """\n'
            '        configured = self.config.extra.get("strict_mention")\n'
            "        if configured is not None:\n"
            "            if isinstance(configured, str):\n"
            '                return configured.lower() in ("true", "1", "yes", "on")\n'
            "            return bool(configured)\n"
            '        return os.getenv("SLACK_STRICT_MENTION", "false").lower() in ("true", "1", "yes", "on")\n'
            "\n"
            "    def _slack_free_response_channels(self) -> set:\n"
        )
        if s.count(helper_old) != 1:
            raise RuntimeError(
                f"slack.py helper anchor not found (count={s.count(helper_old)}) in {slk_path}."
            )
        s = s.replace(helper_old, helper_new)
        slk_path.write_text(s)

    return True


def send_message_edit_action() -> bool:
    """Expose `action='edit'` on the `send_message` tool.

    Wires four insertions into ``tools/send_message_tool.py``:

    1. Expand ``SEND_MESSAGE_SCHEMA``'s ``action`` enum with ``"edit"``,
       retarget the top-level description, refine the ``message``
       description, and add a ``message_id`` property.
    2. Add an ``if action == "edit": return _handle_edit(args)`` branch
       to the top-level ``send_message_tool`` dispatcher.
    3. Insert a ``_handle_edit`` function next to ``_handle_list``. It
       parses/target-resolves the same way as ``_handle_send`` and
       delegates to ``_edit_on_platform`` once it has a ``chat_id``.
    4. Insert ``_edit_on_platform`` + ``_edit_slack`` helpers next to
       ``_send_slack``. Slack is the only platform wired today; other
       platforms return an explicit "not yet implemented" error so
       callers can't silently fall back to a send.
    """
    path = HERMES / "tools" / "send_message_tool.py"
    src = path.read_text()

    if "_handle_edit" in src and "_edit_slack" in src:
        return False

    # ── 1. Schema: enum + descriptions + message_id property ─────────
    schema_old = (
        'SEND_MESSAGE_SCHEMA = {\n'
        '    "name": "send_message",\n'
        '    "description": (\n'
        '        "Send a message to a connected messaging platform, or list available targets.\\n\\n"\n'
        '        "IMPORTANT: When the user asks to send to a specific channel or person "\n'
        '        "(not just a bare platform name), call send_message(action=\'list\') FIRST to see "\n'
        '        "available targets, then send to the correct one.\\n"\n'
        '        "If the user just says a platform name like \'send to telegram\', send directly "\n'
        '        "to the home channel without listing first."\n'
        '    ),\n'
        '    "parameters": {\n'
        '        "type": "object",\n'
        '        "properties": {\n'
        '            "action": {\n'
        '                "type": "string",\n'
        '                "enum": ["send", "list"],\n'
        '                "description": "Action to perform. \'send\' (default) sends a message. \'list\' returns all available channels/contacts across connected platforms."\n'
        '            },\n'
        '            "target": {\n'
        '                "type": "string",\n'
        '                "description": "Delivery target. Format: \'platform\' (uses home channel), \'platform:#channel-name\', \'platform:chat_id\', or \'platform:chat_id:thread_id\' for Telegram topics and Discord threads. Examples: \'telegram\', \'telegram:-1001234567890:17585\', \'discord:999888777:555444333\', \'discord:#bot-home\', \'slack:#engineering\', \'signal:+155****4567\', \'matrix:!roomid:server.org\', \'matrix:@user:server.org\'"\n'
        '            },\n'
        '            "message": {\n'
        '                "type": "string",\n'
        '                "description": "The message text to send"\n'
        '            }\n'
        '        },\n'
        '        "required": []\n'
        '    }\n'
        '}\n'
    )
    schema_new = (
        'SEND_MESSAGE_SCHEMA = {\n'
        '    "name": "send_message",\n'
        '    "description": (\n'
        '        "Send a message to a connected messaging platform, list available targets, "\n'
        '        "or edit a previously-sent message.\\n\\n"\n'
        '        "IMPORTANT: When the user asks to send to a specific channel or person "\n'
        '        "(not just a bare platform name), call send_message(action=\'list\') FIRST to see "\n'
        '        "available targets, then send to the correct one.\\n"\n'
        '        "If the user just says a platform name like \'send to telegram\', send directly "\n'
        '        "to the home channel without listing first.\\n\\n"\n'
        '        "To edit a previously-sent message, use action=\'edit\' with the platform\'s "\n'
        '        "message ID (e.g. the \'ts\' from a prior Slack send\'s response) in message_id. "\n'
        '        "Edit is currently supported on Slack."\n'
        '    ),\n'
        '    "parameters": {\n'
        '        "type": "object",\n'
        '        "properties": {\n'
        '            "action": {\n'
        '                "type": "string",\n'
        '                "enum": ["send", "list", "edit"],\n'
        '                "description": "Action to perform. \'send\' (default) sends a message. \'list\' returns all available channels/contacts across connected platforms. \'edit\' updates the text of a previously-sent message identified by message_id."\n'
        '            },\n'
        '            "target": {\n'
        '                "type": "string",\n'
        '                "description": "Delivery target. Format: \'platform\' (uses home channel), \'platform:#channel-name\', \'platform:chat_id\', or \'platform:chat_id:thread_id\' for Telegram topics and Discord threads. Examples: \'telegram\', \'telegram:-1001234567890:17585\', \'discord:999888777:555444333\', \'discord:#bot-home\', \'slack:#engineering\', \'signal:+155****4567\', \'matrix:!roomid:server.org\', \'matrix:@user:server.org\'"\n'
        '            },\n'
        '            "message": {\n'
        '                "type": "string",\n'
        '                "description": "The message text to send (action=\'send\') or the replacement content for an edit (action=\'edit\')."\n'
        '            },\n'
        '            "message_id": {\n'
        '                "type": "string",\n'
        '                "description": "Required when action=\'edit\'. The platform-native message ID of the message to update. On Slack this is the \'ts\' string returned by the original send (e.g. \'1776830758.467619\'); it\'s also the last path segment of a Slack message link\'s \'p...\' fragment with a \'.\' inserted before the last six digits."\n'
        '            }\n'
        '        },\n'
        '        "required": []\n'
        '    }\n'
        '}\n'
    )
    if src.count(schema_old) != 1:
        raise RuntimeError(
            f"send_message schema anchor not found (count={src.count(schema_old)}) in {path}."
        )
    src = src.replace(schema_old, schema_new)

    # ── 2. Dispatcher: add edit branch ───────────────────────────────
    dispatch_old = (
        'def send_message_tool(args, **kw):\n'
        '    """Handle cross-channel send_message tool calls."""\n'
        '    action = args.get("action", "send")\n'
        '\n'
        '    if action == "list":\n'
        '        return _handle_list()\n'
        '\n'
        '    return _handle_send(args)\n'
    )
    dispatch_new = (
        'def send_message_tool(args, **kw):\n'
        '    """Handle cross-channel send_message tool calls."""\n'
        '    action = args.get("action", "send")\n'
        '\n'
        '    if action == "list":\n'
        '        return _handle_list()\n'
        '\n'
        '    if action == "edit":\n'
        '        return _handle_edit(args)\n'
        '\n'
        '    return _handle_send(args)\n'
    )
    if src.count(dispatch_old) != 1:
        raise RuntimeError(
            f"send_message dispatcher anchor not found (count={src.count(dispatch_old)}) in {path}."
        )
    src = src.replace(dispatch_old, dispatch_new)

    # ── 3. Insert _handle_edit after _handle_list ────────────────────
    handle_list_anchor = (
        'def _handle_list():\n'
        '    """Return formatted list of available messaging targets."""\n'
        '    try:\n'
        '        from gateway.channel_directory import format_directory_for_display\n'
        '        return json.dumps({"targets": format_directory_for_display()})\n'
        '    except Exception as e:\n'
        '        return json.dumps(_error(f"Failed to load channel directory: {e}"))\n'
        '\n'
        '\n'
        'def _handle_send(args):\n'
    )
    handle_edit_block = (
        'def _handle_list():\n'
        '    """Return formatted list of available messaging targets."""\n'
        '    try:\n'
        '        from gateway.channel_directory import format_directory_for_display\n'
        '        return json.dumps({"targets": format_directory_for_display()})\n'
        '    except Exception as e:\n'
        '        return json.dumps(_error(f"Failed to load channel directory: {e}"))\n'
        '\n'
        '\n'
        'def _handle_edit(args):\n'
        '    """Edit a previously-sent message on a platform that supports it.\n'
        '\n'
        '    Target and platform resolution mirror _handle_send; the only extra\n'
        '    input is `message_id`, which the platform adapter needs to address\n'
        '    the existing message. Currently routes to Slack only; other\n'
        '    platforms return an explicit "not yet implemented" error so callers\n'
        '    don\'t silently fall through to a send.\n'
        '    """\n'
        '    target = args.get("target", "")\n'
        '    message_id = args.get("message_id", "")\n'
        '    message = args.get("message", "")\n'
        '    if not target or not message_id or not message:\n'
        '        return tool_error(\n'
        '            "\'target\', \'message_id\', and \'message\' are all required when action=\'edit\'"\n'
        '        )\n'
        '\n'
        '    parts = target.split(":", 1)\n'
        '    platform_name = parts[0].strip().lower()\n'
        '    target_ref = parts[1].strip() if len(parts) > 1 else None\n'
        '    chat_id = None\n'
        '    thread_id = None\n'
        '\n'
        '    if target_ref:\n'
        '        chat_id, thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)\n'
        '    else:\n'
        '        is_explicit = False\n'
        '\n'
        '    # Resolve human-friendly channel names to numeric IDs (same path as send).\n'
        '    if target_ref and not is_explicit:\n'
        '        try:\n'
        '            from gateway.channel_directory import resolve_channel_name\n'
        '            resolved = resolve_channel_name(platform_name, target_ref)\n'
        '            if resolved:\n'
        '                chat_id, thread_id, _ = _parse_target_ref(platform_name, resolved)\n'
        '            else:\n'
        '                return json.dumps({\n'
        '                    "error": f"Could not resolve \'{target_ref}\' on {platform_name}. "\n'
        '                    f"Use send_message(action=\'list\') to see available targets."\n'
        '                })\n'
        '        except Exception:\n'
        '            return json.dumps({\n'
        '                "error": f"Could not resolve \'{target_ref}\' on {platform_name}. "\n'
        '                f"Try using a numeric channel ID instead."\n'
        '            })\n'
        '\n'
        '    from tools.interrupt import is_interrupted\n'
        '    if is_interrupted():\n'
        '        return tool_error("Interrupted")\n'
        '\n'
        '    try:\n'
        '        from gateway.config import load_gateway_config, Platform\n'
        '        config = load_gateway_config()\n'
        '    except Exception as e:\n'
        '        return json.dumps(_error(f"Failed to load gateway config: {e}"))\n'
        '\n'
        '    platform_map = {\n'
        '        "slack": Platform.SLACK,\n'
        '    }\n'
        '    platform = platform_map.get(platform_name)\n'
        '    if not platform:\n'
        '        return tool_error(\n'
        '            f"action=\'edit\' is not yet implemented for platform \'{platform_name}\'. "\n'
        '            f"Currently supported: {\', \'.join(sorted(platform_map.keys()))}."\n'
        '        )\n'
        '\n'
        '    pconfig = config.platforms.get(platform)\n'
        '    if not pconfig or not pconfig.enabled:\n'
        '        return tool_error(\n'
        '            f"Platform \'{platform_name}\' is not configured. "\n'
        '            f"Set up credentials in ~/.hermes/config.yaml or environment variables."\n'
        '        )\n'
        '\n'
        '    # Bare target (e.g. "slack") → home channel, same as send.\n'
        '    # The message_id implicitly identifies a message the bot sent,\n'
        '    # and in the bare-target case that almost always means the home\n'
        '    # channel — matching send\'s semantics keeps the tool symmetric.\n'
        '    if not chat_id:\n'
        '        home = config.get_home_channel(platform)\n'
        '        if home:\n'
        '            chat_id = home.chat_id\n'
        '        else:\n'
        '            return tool_error(\n'
        '                f"No home channel set for {platform_name} and no explicit chat_id "\n'
        '                f"in target. For edit, specify \'{platform_name}:<chat_id>\' so the "\n'
        '                f"edit goes to the same channel as the original message."\n'
        '            )\n'
        '\n'
        '    try:\n'
        '        from model_tools import _run_async\n'
        '        result = _run_async(\n'
        '            _edit_on_platform(platform, pconfig, chat_id, message_id, message)\n'
        '        )\n'
        '        if isinstance(result, dict) and "error" in result:\n'
        '            result["error"] = _sanitize_error_text(result["error"])\n'
        '        return json.dumps(result)\n'
        '    except Exception as e:\n'
        '        return json.dumps(_error(f"Edit failed: {e}"))\n'
        '\n'
        '\n'
        'def _handle_send(args):\n'
    )
    if src.count(handle_list_anchor) != 1:
        raise RuntimeError(
            f"_handle_list anchor not found (count={src.count(handle_list_anchor)}) in {path}."
        )
    src = src.replace(handle_list_anchor, handle_edit_block)

    # ── 4. Insert _edit_on_platform + _edit_slack after _send_slack ──
    slack_send_anchor = (
        'async def _send_slack(token, chat_id, message):\n'
        '    """Send via Slack Web API."""\n'
        '    try:\n'
        '        import aiohttp\n'
        '    except ImportError:\n'
        '        return {"error": "aiohttp not installed. Run: pip install aiohttp"}\n'
        '    try:\n'
        '        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp\n'
        '        _proxy = resolve_proxy_url()\n'
        '        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)\n'
        '        url = "https://slack.com/api/chat.postMessage"\n'
        '        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}\n'
        '        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:\n'
        '            payload = {"channel": chat_id, "text": message, "mrkdwn": True}\n'
        '            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:\n'
        '                data = await resp.json()\n'
        '                if data.get("ok"):\n'
        '                    return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": data.get("ts")}\n'
        '                return _error(f"Slack API error: {data.get(\'error\', \'unknown\')}")\n'
        '    except Exception as e:\n'
        '        return _error(f"Slack send failed: {e}")\n'
        '\n'
        '\n'
        'async def _send_whatsapp(extra, chat_id, message):\n'
    )
    slack_edit_block = (
        'async def _send_slack(token, chat_id, message):\n'
        '    """Send via Slack Web API."""\n'
        '    try:\n'
        '        import aiohttp\n'
        '    except ImportError:\n'
        '        return {"error": "aiohttp not installed. Run: pip install aiohttp"}\n'
        '    try:\n'
        '        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp\n'
        '        _proxy = resolve_proxy_url()\n'
        '        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)\n'
        '        url = "https://slack.com/api/chat.postMessage"\n'
        '        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}\n'
        '        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:\n'
        '            payload = {"channel": chat_id, "text": message, "mrkdwn": True}\n'
        '            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:\n'
        '                data = await resp.json()\n'
        '                if data.get("ok"):\n'
        '                    return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": data.get("ts")}\n'
        '                return _error(f"Slack API error: {data.get(\'error\', \'unknown\')}")\n'
        '    except Exception as e:\n'
        '        return _error(f"Slack send failed: {e}")\n'
        '\n'
        '\n'
        'async def _edit_on_platform(platform, pconfig, chat_id, message_id, message):\n'
        '    """Dispatch an edit to the correct platform helper.\n'
        '\n'
        '    Mirrors `_send_to_platform` but for edits. Only platforms that have\n'
        '    a raw-HTTP edit helper here are supported; others fall through to\n'
        '    an explicit "not yet implemented" error so callers don\'t silently\n'
        '    re-send.\n'
        '    """\n'
        '    from gateway.config import Platform\n'
        '    from gateway.platforms.slack import SlackAdapter\n'
        '\n'
        '    if platform == Platform.SLACK:\n'
        '        # Apply the same mrkdwn formatting the send path uses so bold /\n'
        '        # links render identically after the edit.\n'
        '        try:\n'
        '            slack_adapter = SlackAdapter.__new__(SlackAdapter)\n'
        '            message = slack_adapter.format_message(message)\n'
        '        except Exception:\n'
        '            logger.debug("Failed to apply Slack mrkdwn formatting for edit", exc_info=True)\n'
        '        return await _edit_slack(pconfig.token, chat_id, message_id, message)\n'
        '\n'
        '    return {\n'
        '        "error": (\n'
        '            f"action=\'edit\' is not yet implemented for {platform.value}. "\n'
        '            f"Currently supported: slack."\n'
        '        )\n'
        '    }\n'
        '\n'
        '\n'
        'async def _edit_slack(token, chat_id, message_id, message):\n'
        '    """Edit a Slack message via chat.update."""\n'
        '    try:\n'
        '        import aiohttp\n'
        '    except ImportError:\n'
        '        return {"error": "aiohttp not installed. Run: pip install aiohttp"}\n'
        '    try:\n'
        '        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp\n'
        '        _proxy = resolve_proxy_url()\n'
        '        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)\n'
        '        url = "https://slack.com/api/chat.update"\n'
        '        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}\n'
        '        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:\n'
        '            payload = {"channel": chat_id, "ts": message_id, "text": message, "mrkdwn": True}\n'
        '            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:\n'
        '                data = await resp.json()\n'
        '                if data.get("ok"):\n'
        '                    return {\n'
        '                        "success": True,\n'
        '                        "platform": "slack",\n'
        '                        "chat_id": chat_id,\n'
        '                        "message_id": data.get("ts", message_id),\n'
        '                    }\n'
        '                return _error(f"Slack API error: {data.get(\'error\', \'unknown\')}")\n'
        '    except Exception as e:\n'
        '        return _error(f"Slack edit failed: {e}")\n'
        '\n'
        '\n'
        'async def _send_whatsapp(extra, chat_id, message):\n'
    )
    if src.count(slack_send_anchor) != 1:
        raise RuntimeError(
            f"_send_slack anchor not found (count={src.count(slack_send_anchor)}) in {path}."
        )
    src = src.replace(slack_send_anchor, slack_edit_block)

    path.write_text(src)
    return True


def main() -> None:
    if not HERMES.exists():
        print(f"Hermes not found at {HERMES}; nothing to patch.")
        return
    _apply("slack-strict-mention", slack_strict_mention)
    _apply("send-message-edit-action", send_message_edit_action)


if __name__ == "__main__":
    main()
