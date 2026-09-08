"""Private production transport remains disabled pending isolation acceptance.

Offline safety tests inject FakeBridge into ExecutionEngine. Credentials,
config, environment or HTTP-client injection cannot enable this entry point.
"""


class BridgeError(RuntimeError):
    def __init__(self, message, ambiguous=False, code='BRIDGE_ERROR'):
        super().__init__(message)
        self.ambiguous, self.code = ambiguous, str(code)


class BridgeClient:
    def __init__(self, url, token, timeout=8, client=None, *, config_provider=None):
        raise BridgeError('Production live isolation is not accepted', code='LIVE_ISOLATION_NOT_ACCEPTED')

    async def call(self, operation, **payload):
        raise BridgeError('Production live isolation is not accepted', code='LIVE_ISOLATION_NOT_ACCEPTED')

    async def close(self):
        return None  # Constructor never creates or owns a network client.
