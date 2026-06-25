"""
Notification Provider Registry - Auto-discovery and registration of providers.
"""

from shared.logger import get_logger

from app.notifications.providers.base import BaseNotificationProvider

logger = get_logger("luxswirl.notifications.registry")


class NotificationRegistry:
    """
    Registry for notification providers.

    Provides auto-discovery and registration of notification provider classes.
    Similar to the check registry pattern.
    """

    _providers: dict[str, type[BaseNotificationProvider]] = {}

    @classmethod
    def register(
        cls,
        provider_type: str,
        provider_class: type[BaseNotificationProvider],
    ) -> None:
        """
        Register a notification provider.

        Args:
            provider_type: Unique identifier for this provider type
            provider_class: The provider class (must inherit from BaseNotificationProvider)

        Raises:
            ValueError: If provider_type is already registered or invalid
        """
        if not provider_type:
            raise ValueError("Provider type cannot be empty")

        if provider_type in cls._providers:
            raise ValueError(f"Provider type '{provider_type}' is already registered")

        if not issubclass(provider_class, BaseNotificationProvider):
            raise ValueError(
                f"Provider class must inherit from BaseNotificationProvider, got {provider_class}"
            )

        cls._providers[provider_type] = provider_class
        logger.info(
            "Registered notification provider",
            extra={"provider_type": provider_type},
        )

    @classmethod
    def get(cls, provider_type: str) -> type[BaseNotificationProvider] | None:
        """
        Get a registered provider class by type.

        Args:
            provider_type: The provider type identifier

        Returns:
            Provider class, or None if not found
        """
        return cls._providers.get(provider_type)

    @classmethod
    def get_all(cls) -> dict[str, type[BaseNotificationProvider]]:
        """
        Get all registered providers.

        Returns:
            Dictionary mapping provider types to provider classes
        """
        return cls._providers.copy()

    @classmethod
    def get_provider_types(cls) -> list[str]:
        """
        Get list of all registered provider types.

        Returns:
            List of provider type identifiers
        """
        return list(cls._providers.keys())

    @classmethod
    def get_provider_schemas(cls) -> dict[str, dict]:
        """
        Get configuration schemas for all registered providers.

        Returns:
            Dictionary mapping provider types to their config schemas
        """
        return {
            provider_type: provider_class.get_config_schema()
            for provider_type, provider_class in cls._providers.items()
        }

    @classmethod
    def get_provider_info(cls) -> list[dict]:
        """
        Get information about all registered providers.

        Returns:
            List of provider information dictionaries
        """
        return [
            {
                "type": provider_type,
                "name": provider_class.get_provider_name(),
                "description": provider_class.get_provider_description(),
                "schema": provider_class.get_config_schema(),
            }
            for provider_type, provider_class in cls._providers.items()
        ]

    @classmethod
    def create_provider(
        cls,
        provider_type: str,
        config: dict,
    ) -> BaseNotificationProvider:
        """
        Create a provider instance from configuration.

        Args:
            provider_type: The provider type
            config: Provider configuration

        Returns:
            Initialized provider instance

        Raises:
            ValueError: If provider type not found
        """
        provider_class = cls.get(provider_type)
        if not provider_class:
            available = ", ".join(cls.get_provider_types())
            raise ValueError(f"Unknown provider type: {provider_type}. Available: {available}")

        return provider_class(config)

    @classmethod
    def is_registered(cls, provider_type: str) -> bool:
        """
        Check if a provider type is registered.

        Args:
            provider_type: The provider type to check

        Returns:
            True if registered, False otherwise
        """
        return provider_type in cls._providers

    @classmethod
    def clear(cls) -> None:
        """Clear all registered providers (mainly for testing)."""
        cls._providers.clear()

    @classmethod
    def count(cls) -> int:
        """
        Get count of registered providers.

        Returns:
            Number of registered providers
        """
        return len(cls._providers)
