"""Read-only bridge to the Omni agent harness (PLAN.md A1).

OmniBots uses Omni's providers, keys, models, skills and MCP servers, so the
same setup drives both. Nothing in this package ever writes to Omni's files.
"""

from omnibots.omni.config import OmniConfig, ProviderInfo, load_omni_config
from omnibots.omni.locate import OmniLocation, OmniNotFound, locate_omni

__all__ = ["OmniConfig", "ProviderInfo", "load_omni_config", "OmniLocation", "OmniNotFound", "locate_omni"]
