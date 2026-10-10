from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIRNAME = ".duet"
CONFIG_FILENAME = "config.json"
ENV_FILENAME = ".env"


def config_dir(root: str) -> Path:
    return Path(root).expanduser().resolve() / CONFIG_DIRNAME


def load_env_file(root: str) -> List[str]:
    """Load `.duet/.env` into the process env without clobbering real env vars."""
    path = config_dir(root) / ENV_FILENAME
    loaded: List[str] = []
    if not path.is_file():
        return loaded
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    except OSError:
        pass
    return loaded


# What people type, and what it means. The left of a pair leads.
BACKEND_ALIASES: Dict[str, "tuple"] = {
    "claude": ("claude-code", "claude"),
    "claude-code": ("claude-code", "claude"),
    "cc": ("claude-code", "claude"),
    "codex": ("codex-cli", "chatgpt"),
    "chatgpt": ("codex-cli", "chatgpt"),
    "codex-cli": ("codex-cli", "chatgpt"),
    "openai": ("openai-api", "chatgpt"),
    "gpt": ("openai-api", "chatgpt"),
    "openai-api": ("openai-api", "chatgpt"),
    "mock": ("mock", "mock"),
}

DEFAULT_PAIR = "claude+codex"
# Seconds one run of the acceptance gate may take before it is stopped.
DEFAULT_GATE_TIMEOUT = 900


def ensure_distinct_names(agents: List["AgentSpec"]) -> List["AgentSpec"]:
    """Two agents must never share a name.

    The orchestrator keys adapters by name and the consensus state keys
    sign-offs by name, so two agents called the same thing are one adapter and
    one sign-off slot: the reviewer becomes the session that wrote the code,
    and `len(signoffs) < len(agents)` stays true forever, which makes consensus
    unreachable. The model is only a useful suffix when the models differ.

    Applied to a loaded config as well as a parsed pair, because a config saved
    by an earlier version has the colliding names already written down, and
    nothing would repair them on the way back in.
    """
    if len(agents) < 2 or len({a.name for a in agents}) == len(agents):
        return agents
    for agent in agents:
        # Not twice: a name loaded from a saved config already carries the
        # model, and `claude-sonnet-sonnet-1` helps nobody read a transcript.
        suffix = agent.model.split("/")[-1] if agent.model else ""
        if suffix and not agent.name.endswith("-" + suffix):
            agent.name = "%s-%s" % (agent.name, suffix)
    if len({a.name for a in agents}) != len(agents):
        for index, agent in enumerate(agents, start=1):
            agent.name = "%s-%d" % (agent.name, index)
    return agents


def parse_pair(spec: str) -> List["AgentSpec"]:
    """Turn `claude+codex` — or `codex+claude`, or `claude:opus+claude:sonnet` —
    into two agents. The one on the left takes the first turn.
    """
    parts = [p.strip() for p in str(spec).split("+") if p.strip()]
    if len(parts) != 2:
        raise ValueError(
            "a pair needs exactly two agents, like claude+codex — got %r.\n"
            "Known agents: %s" % (spec, ", ".join(sorted(set(BACKEND_ALIASES))))
        )

    agents: List[AgentSpec] = []
    for part in parts:
        alias, _, model = part.partition(":")
        alias = alias.strip().lower()
        if alias not in BACKEND_ALIASES:
            raise ValueError(
                "unknown agent %r in %r.\nKnown agents: %s"
                % (alias, spec, ", ".join(sorted(set(BACKEND_ALIASES))))
            )
        backend, name = BACKEND_ALIASES[alias]
        agents.append(AgentSpec(name=name, backend=backend, model=model.strip()))

    # Two of the same need telling apart in every transcript and report, and
    # for the harder reason in ensure_distinct_names.
    return ensure_distinct_names(agents)


@dataclass
class AgentSpec:
    name: str
    backend: str
    model: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Config:
    task: str = ""
    acceptance: str = ""
    context: str = ""        # the conversation this task came out of
    root: str = "."
    agents: List[AgentSpec] = field(default_factory=list)
    start: str = ""
    decider: str = ""
    gate: str = ""
    gate_was_detected: bool = False
    # Seconds. Saved with the session like `turn_timeout`, so `--gate-timeout`
    # given to a session is kept by `duet resume`.
    gate_timeout: int = DEFAULT_GATE_TIMEOUT
    # How long one agent's turn may take, in seconds. 0 means each backend's own
    # default. It is on the config — not only on the agent options — so that
    # `--turn-timeout` is saved with the session and `duet resume` keeps it;
    # before this the limit could only be changed by editing state.json by hand.
    turn_timeout: int = 0
    # A command the agents may run that is not the gate: the project's tests,
    # for a session (a plan) whose finish line is not a green test run.
    allow_run: str = ""
    max_rounds: int = 12
    max_debate: int = 3
    stall_limit: int = 3
    swap_every: int = 0
    on_blocked: str = "stop"
    commit: bool = False
    # Which workflow's rules apply ("" for a plain run), and what it has
    # observed so far. Kept on the config because the config is saved after
    # every turn — so a baseline taken at the start, or the fact that a bug
    # was reproduced in round 2, survives a crash and a `duet resume`.
    workflow: str = ""
    workflow_state: Dict[str, Any] = field(default_factory=dict)
    # `--jury`: a simulated stakeholder panel sits when both agents sign off,
    # at most `jury_max` times. How often it has sat is saved here, with the
    # rest of the config, so a resumed session cannot reset the cap.
    jury: bool = False
    jury_panel: str = ""
    jury_max: int = 2
    jury_convened: int = 0

    @property
    def agent_names(self) -> List[str]:
        return [a.name for a in self.agents]

    def agent(self, name: str) -> AgentSpec:
        for spec in self.agents:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def order(self) -> List[str]:
        names = self.agent_names
        if self.start and self.start in names:
            first = self.start
            return [first] + [n for n in names if n != first]
        return names

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["agents"] = [a.to_dict() for a in self.agents]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        data = dict(data or {})
        agents = [
            AgentSpec(**{k: v for k, v in a.items() if k in AgentSpec.__dataclass_fields__})
            for a in data.pop("agents", [])
            if isinstance(a, dict) and a.get("name") and a.get("backend")
        ]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.agents = ensure_distinct_names(agents) if agents else default_agents()
        return cfg


def default_agents(prefer_codex: Optional[bool] = None) -> List[AgentSpec]:
    """Claude Code on one side, ChatGPT on the other — both signed in, not billed.

    Both defaults authenticate with the account you already pay for: `claude auth
    login` for a Claude subscription, `codex login` for a ChatGPT one. No API key
    is involved on either side. The key-based `openai-api` backend stays available
    for anyone who prefers it, but you have to ask for it.
    """
    if prefer_codex is None:
        prefer_codex = True
    return parse_pair(DEFAULT_PAIR if prefer_codex else "claude+openai")


def default_config(root: str = ".") -> Config:
    return Config(root=str(Path(root).expanduser().resolve()), agents=default_agents())


def config_path(root: str) -> Path:
    return config_dir(root) / CONFIG_FILENAME


def load_config(root: str = ".") -> Config:
    path = config_path(root)
    if path.is_file():
        try:
            cfg = Config.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise SystemExit("could not read %s: %s" % (path, exc))
        cfg.root = str(Path(root).expanduser().resolve())
        return cfg
    return default_config(root)


def save_config(cfg: Config, root: Optional[str] = None) -> Path:
    root = root or cfg.root
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.to_dict()
    data.pop("task", None)
    data.pop("acceptance", None)
    data.pop("root", None)  # config travels with the repo; the path does not
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
