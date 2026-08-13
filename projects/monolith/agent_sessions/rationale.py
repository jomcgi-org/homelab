from __future__ import annotations


def rationale_trailer_instruction() -> str:
    """Return the plain-text trailer instruction used by agent sessions."""
    return (
        "End your reply with a plain-text trailer in exactly this shape:\n"
        "\nRATIONALE\n"
        "- path: <repo-relative path> · why: <one or two sentences>\n"
        "- path: ... (repeat per important path, most important first)\n"
        "- deviation: <anything you did differently from the task, and why> (zero or more)\n"
        "\nKeep it under 12 lines. Do not use markdown formatting inside the trailer."
    )
