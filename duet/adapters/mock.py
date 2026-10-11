"""A scripted peer.

`duet demo` and the test suite run the entire orchestrator — envelopes, issues,
arbitration, double signoff — with no API key and no network, so the loop can be
verified on its own terms.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from duet.adapters.base import Adapter, AgentReply, Probe


def envelope(message: str, verdict: str = "CONTINUE", **kwargs) -> str:
    body: Dict[str, Any] = {"message": message, "verdict": verdict}
    body.update(kwargs)
    return "%s\n\n```json\n%s\n```" % (message, json.dumps(body, indent=2))


class MockAdapter(Adapter):
    backend = "mock"
    display = "mock"
    edits_workspace = False

    def __init__(self, *args, script: Optional[List[str]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.script: List[str] = list(script or self.config.get("script") or [])
        # A mock standing in for an agent that has its own tools: these files are
        # written during send(), the way a real backend would write them.
        self.writes: Dict[str, str] = dict(self.config.get("writes") or {})
        if self.writes:
            self.edits_workspace = True
        self.turns = 0
        self.prompts: List[str] = []
        self.systems: List[str] = []
        self.web = False

    def allow_web(self) -> str:
        # Recorded, so a test can see which passes were given the web.
        self.web = True
        return "web tools allowed (mock)"

    def send(self, prompt: str, system: str = "", round_no: int = 0) -> AgentReply:
        self.prompts.append(prompt)
        self.systems.append(system)
        for rel, content in self.writes.items():
            path = Path(self.cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        # A scripted backend failure. Real backends fail (not signed in, no
        # network, rate limited), and they sometimes fail *with* a reply — a
        # truncated answer, a retry that half worked. `error` on its own is a
        # dead backend; `error` alongside a script is a degraded reply that the
        # caller still has to notice.
        failure = str(self.config.get("error") or "")
        if failure and not self.script:
            self.turns += 1
            return AgentReply(text="", error=failure, meta={"backend": "mock", "turn": self.turns})
        if self.turns < len(self.script):
            text = self.script[self.turns]
        elif self.script:
            text = self.script[-1]
        else:
            text = envelope("mock %s has nothing to add" % self.name, "DONE", confidence=0.9)
        self.turns += 1
        return AgentReply(text=text, error=failure, meta={"backend": "mock", "turn": self.turns})

    def state(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "turns": self.turns}

    def restore(self, state: Dict[str, Any]) -> None:
        # Where in the script this peer had got to. A real backend resumes its
        # own thread across a resumed session; a mock that restarted its script
        # would make the loop look resumable when it was only repeating itself.
        super().restore(state)
        try:
            self.turns = int((state or {}).get("turns") or 0)
        except (TypeError, ValueError):
            self.turns = 0

    @classmethod
    def probe(cls, config: Optional[Dict[str, Any]] = None) -> Probe:
        return Probe(ok=True, detail="mock backend is always available")
