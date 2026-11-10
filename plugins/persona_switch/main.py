from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class PersonaSwitch(Star):
    """给每个群（会话）单独指定人设：/人设 [名字]。"""

    def __init__(self, context: Context):
        super().__init__(context)

    def _names(self) -> list[str]:
        pm = getattr(self.context, "persona_manager", None)
        personas = getattr(pm, "personas", None) or []
        return [p.persona_id for p in personas]

    @filter.command("人设")
    async def switch(self, event: AstrMessageEvent):
        if getattr(event, "role", "member") != "admin":
            yield event.plain_result("你谁啊，这命令不归你管。")
            return

        text = event.message_str.strip()
        text = text[3:].strip() if text.startswith("/人设") else text[2:].strip()
        names = self._names()

        if not text:
            yield event.plain_result("可用人设：" + "、".join(names))
            return

        if text not in names:
            yield event.plain_result(f"没这人设。有：{'、'.join(names)}")
            return

        umo = event.unified_msg_origin
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        conv = await cm.get_conversation(umo, cid) if cid else None
        if conv is None:
            # 会话不存在（或已被删）就地新建，避免指向已删除的对话
            await cm.new_conversation(umo, event.get_platform_id(), persona_id=text)
        else:
            await cm.update_conversation(umo, cid, persona_id=text)

        yield event.plain_result(f"这儿的规矩换了：{text}")
