"""Exceptions the API maps to HTTP answers.

NotFound is the one deliberate "no such thing" error (HTTP 404). It subclasses KeyError so existing
callers that catch KeyError keep working, but the API maps only NotFound to 404: a plain KeyError is a
bug or a missing field, never "not found".
"""


class NotFound(KeyError):
    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "not found"
