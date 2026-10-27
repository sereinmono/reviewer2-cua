"""Generic OpenAI-compatible VLM Actor for nano-cua.

The actor is action-space agnostic: an action-space group supplies the system
prompt and the proposal parser (``payload, observation -> Proposal``).
"""

from .actor import ProposalParser, VLMActor, parse_json_object

__all__ = ["ProposalParser", "VLMActor", "parse_json_object"]
