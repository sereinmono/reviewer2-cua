"""External two-gate pilot for nano-cua.

This package implements an Observe Gate (before main VLM reasoning) and an
AskHuman Gate (after the candidate action, before execution) entirely outside
the nano-cua base. The only base integration point is an external
``AgentPolicy`` implementation (``GatedPolicy``) plus public ports/messages
injected at composition time.
"""

__version__ = "0.1.0"
