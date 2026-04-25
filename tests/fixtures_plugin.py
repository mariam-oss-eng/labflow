"""Tiny test plugin used to exercise the plugin loader."""

class _Plugin:
    def register(self, api):
        api.add_extractor("noop", lambda text: {"decisions": [], "tasks": []})
        api.declare("test-plugin", version="0.0.1")


plugin = _Plugin()
