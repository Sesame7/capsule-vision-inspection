"""Application exceptions; SDK exceptions remain available through __cause__."""


class VimoError(RuntimeError):
    """ViMo engine lifecycle or operation failed."""


class VimoInitializationError(VimoError):
    """Model staging, SDK loading or module initialization failed."""


class VimoInferenceError(VimoError):
    """The SDK did not complete an inference call."""


class VimoResponseError(VimoError):
    """The returned response cannot be represented by the core contract."""
