"""Compatibility launcher for OKVEJ Bot v20.

Railway should normally run ``python bot.py``. This file remains so an old
``python start.py`` command also starts the same integrated v20 bot safely.
"""

import asyncio
import bot


if __name__ == "__main__":
    asyncio.run(bot.main())
