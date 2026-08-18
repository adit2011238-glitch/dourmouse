"""Research-mesh qualification agents.

A field-agent studies its real exam corpus for a timeboxed window, then takes
every available iteration of its real test one at a time — held-out papers
never enter study material, every attempt is graded deterministically (answer
key where published, citation gate always), and failures trigger focused
remediation with a hard exclusion after three consecutive misses.

Modules:

- core:      the lifecycle state machine (pure logic)
- store:     SQLite persistence, resumable
- brain:     the reasoning seam (MockBrain for offline runs)
- study:     the 5-hour study window + held-out enforcement
- exams:     iterations, deterministic grading, citation gate
- pipeline:  step-driven orchestrator + CLI
"""

from .core import AgentRecord, Status, StudyDossier
from .store import AgentStore

__all__ = ["AgentRecord", "AgentStore", "Status", "StudyDossier"]
