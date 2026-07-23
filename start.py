"""Safe launcher for the existing OKVEJ bot.

The original bot.py remains unchanged. This launcher imports it, adds the
analytics/SEO router and starts the existing polling function.
"""

import asyncio
import logging

import bot as existing_bot
from analytics_router import add_admin_buttons, router


def configure():
    # Register the new handlers before polling starts.
    existing_bot.dp.include_router(router)

    # Existing start handler reads bot.main_menu at request time, so replacing
    # this global keeps all old buttons and adds the private analytics row.
    if hasattr(existing_bot, "main_menu"):
        existing_bot.main_menu = add_admin_buttons(existing_bot.main_menu)


async def run():
    configure()

    if hasattr(existing_bot, "main"):
        await existing_bot.main()
        return

    # Fallback for bot versions without a main() function.
    await existing_bot.dp.start_polling(existing_bot.bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
