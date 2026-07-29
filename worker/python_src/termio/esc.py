from __future__ import annotations

from .ansi import ESC


# ---------------------------------------------------------------------------
# C0 control characters (0x00-0x1f)
# ---------------------------------------------------------------------------

C0 = {
    "NUL": "\x00",  # Null
    "SOH": "\x01",  # Start of Heading
    "STX": "\x02",  # Start of Text
    "ETX": "\x03",  # End of Text
    "EOT": "\x04",  # End of Transmission
    "ENQ": "\x05",  # Enquiry
    "ACK": "\x06",  # Acknowledge
    "BEL": "\x07",  # Bell
    "BS": "\x08",   # Backspace
    "HT": "\x09",   # Horizontal Tab
    "LF": "\x0a",   # Line Feed
    "VT": "\x0b",   # Vertical Tab
    "FF": "\x0c",   # Form Feed
    "CR": "\x0d",   # Carriage Return
    "SO": "\x0e",   # Shift Out
    "SI": "\x0f",   # Shift In
    "DLE": "\x10",  # Data Link Escape
    "DC1": "\x11",  # Device Control 1 (XON)
    "DC2": "\x12",  # Device Control 2
    "DC3": "\x13",  # Device Control 3 (XOFF)
    "DC4": "\x14",  # Device Control 4
    "NAK": "\x15",  # Negative Acknowledge
    "SYN": "\x16",  # Synchronous Idle
    "ETB": "\x17",  # End of Transmission Block
    "CAN": "\x18",  # Cancel
    "EM": "\x19",   # End of Medium
    "SUB": "\x1a",  # Substitute
    "ESC": "\x1b",  # Escape
    "FS": "\x1c",   # File Separator
    "GS": "\x1d",   # Group Separator
    "RS": "\x1e",   # Record Separator
    "US": "\x1f",   # Unit Separator
}


# ---------------------------------------------------------------------------
# ESC type constants (escape sequence intermediates/finals)
# ---------------------------------------------------------------------------

ESC_TYPE = {
    "CSI": "[",   # Control Sequence Introducer
    "OSC": "]",   # Operating System Command
    "DCS": "P",  # Device Control String
    "ST": "\\",  # String Terminator
    "APC": "_",  # Application Program Command
    "PM": "^",   # Privacy Message
    "SOS": "X",  # Start of String
}


def sequence(command: str) -> str:
    return f"{ESC}{command}"


def is_c0(codepoint: int) -> bool:
    """Check if a codepoint is a C0 control character (0x00-0x1f)."""
    return 0 <= codepoint <= 0x1f


def is_esc_final(byte: int) -> bool:
    """Check if a byte is a valid escape sequence final byte.

    Final bytes for escape sequences are in the range 0x30-0x7e.
    """
    return 0x30 <= byte <= 0x7e
