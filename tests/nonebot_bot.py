"""NoneBot2 test bot used by e2e_framework.py and e2e_redteam_chain.py.

pip install "nonebot2[fastapi]" nonebot-adapter-onebot
SUPERUSERS='["<admin user_id from e2e_redteam_chain.py --admin-id>"]' python tests/nonebot_bot.py
"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter, MessageEvent, GroupMessageEvent
from nonebot import on_command
nonebot.init(host="127.0.0.1", port=8080, command_start=["/", "!"], driver="~fastapi")
nonebot.get_driver().register_adapter(Adapter)
from nonebot.permission import SUPERUSER
sudo = on_command("sudo", permission=SUPERUSER)
@sudo.handle()
async def _s():
    await sudo.finish("sudo ok")
h = on_command("help")
@h.handle()
async def _(event: MessageEvent):
    where = "group" if isinstance(event, GroupMessageEvent) else "private"
    await h.finish(f"nonebot2 help ok ({where}, user {event.user_id})")
if __name__ == "__main__":
    nonebot.run()
