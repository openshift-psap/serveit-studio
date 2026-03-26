"""
AWS infrastructure provider.

TODO: Not yet implemented. Placeholder for future development.
"""

import logging
from ..base import BaseProvider, ProviderProfile

logger = logging.getLogger(__name__)


class AWSProvider(BaseProvider):
    """AWS infrastructure provider (TODO)."""

    def get_provider_id(self) -> str:
        return "aws"

    def get_display_name(self) -> str:
        return "AWS"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect AWS (TODO: implement detection).

        Could detect via:
        - Node labels (eks.amazonaws.com)
        - Instance metadata
        - Cloud provider config
        """
        # TODO: Implement AWS detection
        return False

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """Load AWS profile (TODO)."""
        raise NotImplementedError("AWS provider not yet implemented")
