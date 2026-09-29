"""User-facing errors that contain no credentials or raw model output."""


class AgentDeliveryError(Exception):
    """Expected failure with a concise, safe message for the operator."""
