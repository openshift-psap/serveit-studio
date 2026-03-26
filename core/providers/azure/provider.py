"""
Azure infrastructure provider.

TODO: Not yet implemented. Placeholder for future development.
"""

import logging
from ..base import BaseProvider, ProviderProfile

logger = logging.getLogger(__name__)


class AzureProvider(BaseProvider):
    """Azure infrastructure provider (TODO)."""

    def get_provider_id(self) -> str:
        return "azure"

    def get_display_name(self) -> str:
        return "Azure"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect Azure (TODO: implement detection).

        Could detect via:
        - Node labels (kubernetes.azure.com)
        - Instance metadata
        - Cloud provider config
        """
        # TODO: Implement Azure detection
        return False

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """Load Azure profile (TODO)."""
        raise NotImplementedError("Azure provider not yet implemented")
