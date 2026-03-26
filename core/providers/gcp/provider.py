"""
Google Cloud Platform (GCP) infrastructure provider.

TODO: Not yet implemented. Placeholder for future development.
"""

import logging
from ..base import BaseProvider, ProviderProfile

logger = logging.getLogger(__name__)


class GCPProvider(BaseProvider):
    """GCP infrastructure provider (TODO)."""

    def get_provider_id(self) -> str:
        return "gcp"

    def get_display_name(self) -> str:
        return "Google Cloud"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect GCP (TODO: implement detection).

        Could detect via:
        - Node labels (cloud.google.com/gke-*)
        - Instance metadata
        - Cloud provider config
        """
        # TODO: Implement GCP detection
        return False

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """Load GCP profile (TODO)."""
        raise NotImplementedError("GCP provider not yet implemented")
