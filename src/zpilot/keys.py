"""Shared key-name to zellij action-write byte-value mapping.

Used by both the web app and the MCP HTTP server to translate
human-readable key names (e.g. "ctrl_c") into the numeric codes
that `zellij action write` expects.
"""

KEY_MAP: dict[str, str] = {
    "enter": "10",
    "tab": "9",
    "escape": "27",
    "backspace": "127",
    "arrow_up": "27 91 65",
    "arrow_down": "27 91 66",
    "arrow_right": "27 91 67",
    "arrow_left": "27 91 68",
    "home": "27 91 72",
    "end": "27 91 70",
    "page_up": "27 91 53 126",
    "page_down": "27 91 54 126",
    "insert": "27 91 50 126",
    "delete": "27 91 51 126",
    "ctrl_c": "3",
    "ctrl_d": "4",
    "ctrl_z": "26",
    "ctrl_l": "12",
    "ctrl_a": "1",
    "ctrl_e": "5",
    "ctrl_r": "18",
    "ctrl_u": "21",
    "ctrl_w": "23",
    # Function keys
    "f1": "27 79 80",
    "f2": "27 79 81",
    "f3": "27 79 82",
    "f4": "27 79 83",
    "f5": "27 91 49 53 126",
    "f6": "27 91 49 55 126",
    "f7": "27 91 49 56 126",
    "f8": "27 91 49 57 126",
    "f9": "27 91 50 48 126",
    "f10": "27 91 50 49 126",
    "f11": "27 91 50 51 126",
    "f12": "27 91 50 52 126",
}


def map_key_to_zellij(key: str) -> str | None:
    """Map a key name to zellij 'action write' byte values.

    Returns None if the key is not recognized.
    """
    return KEY_MAP.get(key.lower())
