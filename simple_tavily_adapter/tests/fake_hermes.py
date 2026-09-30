"""A fake `hermes acp`: a real ACP agent over stdio that misbehaves on demand.

The orchestrator's end-to-end tests (test_invariants.py) run it instead of Hermes.
Every mode reproduces one failure we have met in production runs, or checks one
contract the orchestrator must keep. The mode comes from FAKE_HERMES_MODE.

  report            write report.md with sources, say a few words, end the turn
  huge_line         send one ~3 MB message chunk first (the 64 KB line limit bug)
  die_in_prompt     get killed by SIGKILL in the middle of the turn (OOM kill)
  die_after_session answer new_session, then get killed before the prompt
  hang_init         never answer initialize
  short_reply       answer with a short chat line and no report.md
  loop_text         repeat the same line forever until cancelled; the wrap-up
                    turn then writes report.md
  same_error        log one tool error to stderr over and over, then hang
  hang              say nothing and wait until cancelled
  kill_group        SIGTERM its whole process group, then write report.md
  env               dump the environment it got into env.json, then report.md
  write_outside     ask permission to write outside the workspace, then inside
"""

import asyncio
import json
import os
import signal
import sys

from acp import (
    PROTOCOL_VERSION, run_agent, text_block, update_agent_message_text,
)
from acp.interfaces import Agent
from acp.schema import (
    AgentCapabilities, InitializeResponse, NewSessionResponse, PermissionOption,
    PromptResponse, ToolCallUpdate,
)

MODE = os.environ.get("FAKE_HERMES_MODE", "report")
REPORT = "ANSWER: 42\n\n# Report\n\nSources: https://a.example/one https://b.example/two\n" + ("text " * 200)


class Fake(Agent):
    def __init__(self):
        self.conn = None
        self.cwd = "."
        self.turns = 0
        self.cancelled = asyncio.Event()

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kw):
        if MODE == "hang_init":
            await asyncio.sleep(3600)
        return InitializeResponse(protocol_version=PROTOCOL_VERSION, agent_capabilities=AgentCapabilities())

    async def new_session(self, cwd, mcp_servers=None, **kw):
        self.cwd = cwd
        if MODE == "die_after_session":
            asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGKILL)
        return NewSessionResponse(session_id="fake-session-1")

    async def cancel(self, session_id, **kw):
        self.cancelled.set()

    async def say(self, session_id, text):
        await self.conn.session_update(session_id=session_id, update=update_agent_message_text(text))

    def write_report(self):
        with open(os.path.join(self.cwd, "report.md"), "w") as f:
            f.write(REPORT)

    async def prompt(self, prompt, session_id, message_id=None, **kw):
        self.turns += 1
        if MODE == "die_after_session":
            await asyncio.sleep(30)  # the kill scheduled in new_session lands first
        if MODE == "huge_line":
            await self.say(session_id, "x" * 3_000_000)
        elif MODE == "die_in_prompt":
            await self.say(session_id, "working")
            os.kill(os.getpid(), signal.SIGKILL)
        elif MODE == "short_reply":
            await self.say(session_id, "Round 1 dispatched.")
            return PromptResponse(stop_reason="end_turn")
        elif MODE == "loop_text" and self.turns == 1:
            while not self.cancelled.is_set():
                await self.say(session_id, "I will now search for the latest information about it.\n")
                await asyncio.sleep(0.01)
            return PromptResponse(stop_reason="cancelled")
        elif MODE == "hang":
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")
        elif MODE == "same_error":
            for _ in range(20):
                print("2026-09-29 10:00:00 [WARNING] agent.tool_executor: Tool terminal returned error "
                      "(0.01s): {\"output\": \"No such file 17\"}", file=sys.stderr, flush=True)
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")
        elif MODE == "env":
            keys = ("HERMES_WRITE_SAFE_ROOT", "HERMES_SINGLE_QUERY_SESSION", "SEARCHARVESTER_JOB_ID",
                    "SEARCHARVESTER_URL", "HERMES_HOME")
            with open(os.path.join(self.cwd, "env.json"), "w") as f:
                json.dump({k: os.environ.get(k) for k in keys} | {"pgid_is_own": os.getpgid(0) == os.getpid()}, f)
        elif MODE == "write_outside":
            opts = [PermissionOption(option_id="allow_once", name="Allow", kind="allow_once"),
                    PermissionOption(option_id="reject_once", name="Reject", kind="reject_once")]
            verdicts = {}
            for name, path in (("outside", "/etc/passwd"), ("inside", os.path.join(self.cwd, "report.md"))):
                r = await self.conn.request_permission(
                    options=opts, session_id=session_id,
                    tool_call=ToolCallUpdate(tool_call_id=f"tc-{name}", kind="edit", raw_input={"path": path}))
                verdicts[name] = getattr(r.outcome, "outcome", None)
            with open(os.path.join(self.cwd, "verdicts.json"), "w") as f:
                json.dump(verdicts, f)
        self.write_report()
        await self.say(session_id, "done")
        return PromptResponse(stop_reason="end_turn")


def main():
    if MODE == "kill_group":
        # Hermes cleans up with killpg; in the adapter's group that took uvicorn down.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGTERM)
    asyncio.run(run_agent(Fake()))


if __name__ == "__main__":
    main()
