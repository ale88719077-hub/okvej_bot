import asyncio
import logging

import bot as existing_bot
from analytics_router import add_admin_buttons, router as analytics_router


def configure() -> None:
    existing_bot.dp.include_router(analytics_router)

    if hasattr(existing_bot, "main_menu"):
        existing_bot.main_menu = add_admin_buttons(existing_bot.main_menu)

    logging.info("Analytics/SEO router registered")


async def run() -> None:
    configure()

    main_func = getattr(existing_bot, "main", None)
    if callable(main_func):
        await main_func()
        return

    bot_instance = getattr(existing_bot, "bot", None)
    dispatcher = getattr(existing_bot, "dp", None)

    if bot_instance is None or dispatcher is None:
        raise RuntimeError("bot.py must expose bot and dp objects")

    await dispatcher.start_polling(bot_instance)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
