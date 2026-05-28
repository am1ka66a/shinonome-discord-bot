import typing

import discord


class LinePagerView(discord.ui.View):
    def __init__(self, owner_id, title, lines, page_size=10, start_page=1, color=0x2b2d31, footer_prefix=""):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.title = title
        self.lines = lines
        self.page_size = max(1, page_size)
        self.total_pages = max(1, (len(lines) + self.page_size - 1) // self.page_size)
        self.page = max(1, min(start_page, self.total_pages))
        self.color = color
        self.footer_prefix = footer_prefix
        self.message = None
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages

    def build_embed(self):
        start = (self.page - 1) * self.page_size
        end = start + self.page_size
        body = "\n".join(self.lines[start:end]) or "無資料"
        embed = discord.Embed(title=self.title, description=body, color=self.color)
        footer = f"第 {self.page}/{self.total_pages} 頁"
        if self.footer_prefix:
            footer += f" | {self.footer_prefix}"
        embed.set_footer(text=footer)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("這不是你的翻頁面板。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
