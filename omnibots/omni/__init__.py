"""Bridge to the Omni agent harness (PLAN.md A1).

OmniBots uses Omni's providers, keys, models, skills and MCP servers, so the
same setup drives both. The only write is Settings → Providers
(`providers_store.py`, A11.p.01): it updates provider entries in Omni's
settings.json and leaves every other Omni file alone.
"""

from omnibots.omni.config import OmniConfig, ProviderInfo, load_omni_config
from omnibots.omni.locate import OmniLocation, OmniNotFound, locate_omni

__all__ = ["OmniConfig", "ProviderInfo", "load_omni_config", "OmniLocation", "OmniNotFound", "locate_omni"]
