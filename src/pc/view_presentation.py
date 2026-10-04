"""终端正文显示；清除可执行的控制序列。"""

from __future__ import annotations

import unicodedata


def terminal_text(text: str) -> str:
    return "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")
