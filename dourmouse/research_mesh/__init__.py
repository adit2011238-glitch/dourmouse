"""Research-mesh qualification agents.

A field-agent studies its real exam corpus for a timeboxed window, then takes
every available iteration of its real test one at a time -- held-out papers
never enter study material, every attempt is graded deterministically (answer
key where published, citation gate always), and failures trigger focused
remediation with a hard exclusion after three consecutive misses.

Rebuilt 2026-09-19 from the orphaned ``jarvis/research_mesh/agents`` package
(disconnected from the live product, no real reasoning backend, stale a
month). The pure state-machine logic below is unchanged in substance -- it
was already real and already tested -- what changed: a real ``RealBrain``
backed by this codebase's own real model routing (``dourmouse.chat
.ChatSession``), and real integration as a callable subagent
(``dourmouse/research_mesh_tools.py``) instead of a standalone CLI script
nothing in the product ever called.

Modules:

- core:      the lifecycle state machine (pure logic)
- store:     SQLite persistence, resumable, workspace-relative by default
- brain:     the reasoning seam (MockBrain for offline runs, RealBrain for
             a genuine model-backed agent)
- study:     the 5-hour study window + held-out enforcement
- exams:     iterations, deterministic grading, citation gate
- pipeline:  step-driven orchestrator + CLI
"""

from .core import AgentRecord, Status, StudyDossier
from .store import AgentStore

__all__ = ["AgentRecord", "AgentStore", "Status", "StudyDossier"]
