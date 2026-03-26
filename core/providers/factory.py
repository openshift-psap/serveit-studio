"""
Provider factory and auto-detection.

This module provides the registry of all available providers and auto-detection logic.
"""

import logging
from typing import Optional, List, Type

from .base import BaseProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Registry of all available infrastructure providers.

    Providers are auto-detected in order of specificity:
    1. Cloud providers (IBM Cloud, AWS, GCP, Azure)
    2. Bare metal (default fallback)
    """

    # Lazy-loaded to avoid circular imports
    _providers: Optional[List[Type[BaseProvider]]] = None

    @classmethod
    def _load_providers(cls) -> List[Type[BaseProvider]]:
        """
        Lazy-load all provider classes.

        Returns:
            List of provider classes in detection order
        """
        if cls._providers is not None:
            return cls._providers

        providers = []

        # Try to import each provider (gracefully handle missing providers)
        try:
            from .ibm_cloud.provider import IBMCloudProvider
            providers.append(IBMCloudProvider)
        except ImportError:
            logger.debug("IBM Cloud provider not available")

        try:
            from .aws.provider import AWSProvider
            providers.append(AWSProvider)
        except ImportError:
            logger.debug("AWS provider not available")

        try:
            from .gcp.provider import GCPProvider
            providers.append(GCPProvider)
        except ImportError:
            logger.debug("GCP provider not available")

        try:
            from .azure.provider import AzureProvider
            providers.append(AzureProvider)
        except ImportError:
            logger.debug("Azure provider not available")

        try:
            from .coreweave.provider import CoreWeaveProvider
            providers.append(CoreWeaveProvider)
        except ImportError:
            logger.debug("CoreWeave provider not available")

        try:
            from .baremetal.provider import BaremetalProvider
            providers.append(BaremetalProvider)
        except ImportError:
            logger.warning("Baremetal provider not available (this is required as fallback)")

        cls._providers = providers
        return providers

    @classmethod
    def get_all_providers(cls) -> List[Type[BaseProvider]]:
        """Get all registered provider classes."""
        return cls._load_providers()

    @classmethod
    def get_provider_names(cls) -> List[str]:
        """Get list of all provider IDs."""
        providers = cls.get_all_providers()
        return [p("default").get_provider_id() for p in providers]

    @classmethod
    def get_provider_by_id(
        cls,
        provider_id: str,
        profile: str = "default"
    ) -> Optional[BaseProvider]:
        """
        Get provider instance by ID.

        Args:
            provider_id: Provider identifier (e.g., 'ibm_cloud', 'aws')
            profile: Profile name to load

        Returns:
            Provider instance or None if not found
        """
        providers = cls.get_all_providers()

        for provider_class in providers:
            instance = provider_class(profile=profile)
            if instance.get_provider_id() == provider_id:
                return instance

        logger.warning(f"Provider '{provider_id}' not found")
        return None

    @classmethod
    def detect_provider(
        cls,
        kubectl_runner=None,
        override: Optional[str] = None,
        profile: str = "default"
    ) -> BaseProvider:
        """
        Auto-detect the active infrastructure provider.

        Detection order:
        1. Check for manual override
        2. Try each cloud provider's detect() method
        3. Auto-detect DRANET for IBM Cloud (bypasses pod-per-node constraints)
        4. Fall back to baremetal

        Args:
            kubectl_runner: KubectlRunner for API queries
            override: Optional provider ID to force
            profile: Profile name to load (auto-selected for IBM Cloud if DRANET detected)

        Returns:
            Detected provider instance (always returns a provider)
        """
        # Check for manual override
        if override:
            provider = cls.get_provider_by_id(override, profile=profile)
            if provider:
                logger.info(
                    f"Using manually specified provider: {provider.get_display_name()} "
                    f"(profile: {profile})"
                )
                return provider
            logger.warning(f"Override provider '{override}' not found, falling back to auto-detection")

        # Auto-detect
        providers = cls.get_all_providers()
        for provider_class in providers:
            try:
                # Special handling for IBM Cloud: auto-detect DRANET
                if provider_class.__name__ == "IBMCloudProvider" and profile == "default":
                    # First check if this is IBM Cloud
                    test_instance = provider_class(profile="default")
                    if test_instance.detect(kubectl_runner):
                        # IBM Cloud detected! Check for DRANET
                        if provider_class.detect_dranet(kubectl_runner):
                            # DRANET available - use dranet profile
                            instance = provider_class(profile="dranet")
                            logger.info(
                                f"Detected provider: {instance.get_display_name()} with DRANET enabled "
                                f"(profile: {instance.profile_name}) - bypassing pod-per-node constraints"
                            )
                        else:
                            # No DRANET - use default profile
                            instance = test_instance
                            logger.info(
                                f"Detected provider: {instance.get_display_name()} "
                                f"(profile: {instance.profile_name}) - standard constraints apply"
                            )
                        return instance
                else:
                    # Standard detection for other providers
                    instance = provider_class(profile=profile)
                    if instance.detect(kubectl_runner):
                        logger.info(
                            f"Detected provider: {instance.get_display_name()} "
                            f"(profile: {instance.profile_name})"
                        )
                        return instance
            except Exception as e:
                logger.debug(f"Error detecting {provider_class.__name__}: {e}")

        # Default to baremetal
        logger.info("No cloud provider detected, using baremetal")
        try:
            from .baremetal.provider import BaremetalProvider
            return BaremetalProvider(profile=profile)
        except ImportError:
            # Critical error - no fallback available
            raise RuntimeError(
                "No providers available! At minimum, baremetal provider is required."
            )

    @classmethod
    def list_available_providers(cls) -> List[dict]:
        """
        List all available providers with their profiles.

        Returns:
            List of dicts with provider info: [{'id': 'ibm_cloud', 'name': 'IBM Cloud', 'profiles': [...]}]
        """
        providers = cls.get_all_providers()
        result = []

        for provider_class in providers:
            try:
                instance = provider_class()
                result.append({
                    'id': instance.get_provider_id(),
                    'name': instance.get_display_name(),
                    'profiles': instance.get_available_profiles()
                })
            except Exception as e:
                logger.debug(f"Error listing {provider_class.__name__}: {e}")

        return result
