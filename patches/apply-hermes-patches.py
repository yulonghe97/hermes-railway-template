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


def main() -> None:
    if not HERMES.exists():
        print(f"Hermes not found at {HERMES}; nothing to patch.")
        return
    _apply("slack-strict-mention", slack_strict_mention)


if __name__ == "__main__":
    main()
