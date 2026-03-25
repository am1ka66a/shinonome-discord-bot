from PIL import Image, ImageDraw, ImageFont
import os

OUTPUT_DIR = "cards"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 尺寸與圓角設定 (符合大且清晰乾淨的風格)
W, H = 150, 210
RADIUS = 10

# 顏色對應
COLOR_BLACK = (20, 20, 20)
COLOR_RED   = (225, 40, 40)

SUITS = [
    ('♠', 'S', COLOR_BLACK),
    ('♥', 'H', COLOR_RED),
    ('♦', 'D', COLOR_RED),
    ('♣', 'C', COLOR_BLACK),
]

RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
RANK_CODE = {r: r for r in RANKS}
RANK_CODE['10'] = '0'

def get_sys_font(size, bold=False):
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except:
            pass
    return ImageFont.load_default()

# 字型設定
FONT_RANK = get_sys_font(42, bold=True)
FONT_SUIT_LG = get_sys_font(90)   # 中央超大花色
FONT_JOKER = get_sys_font(70)

def draw_clean_card(rank: str, suit_sym: str, color: tuple) -> Image.Image:
    """完美還原截圖的極簡風格：左上角點數、中央巨大花色"""
    img = Image.new('RGBA', (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(img)

    # 白底圓角卡牌，帶一點淡淡灰色邊框
    draw.rounded_rectangle([0, 0, W-1, H-1], radius=RADIUS, fill='white', outline='#DDDDDD', width=2)
    
    # 左上角數字
    # 稍微留邊距
    draw.text((15, 5), rank, font=FONT_RANK, fill=color, anchor="lt")
    
    # 中央大花色
    draw.text((W//2, H//2 + 10), suit_sym, font=FONT_SUIT_LG, fill=color, anchor="mm")
    
    return img

def draw_joker_back() -> Image.Image:
    """還原截圖中的牌背：深色底 + 鬼牌小丑 / 特定圖案"""
    img = Image.new('RGBA', (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    # 深灰色底
    draw.rounded_rectangle([0, 0, W-1, H-1], radius=RADIUS, fill='#4A4A5A', outline='#2A2A3A', width=2)
    
    # 簡單畫一個白色內框
    draw.rounded_rectangle([8, 8, W-9, H-9], radius=6, outline='#FFFFFF', width=2)
    
    # 中央畫一個 🃏 (使用內建表情文字或替代圖案)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\seguiemj.ttf", 60)
        draw.text((W//2, H//2), "🃏", font=font, fill="black", anchor="mm")
    except:
        # 如果無法渲染 emoji font，畫一個大大的 B 代替牌背
        font = get_sys_font(60, bold=True)
        draw.text((W//2, H//2), "Back", font=font, fill="white", anchor="mm")
        
    return img

def main():
    count = 0
    for suit_sym, suit_code, color in SUITS:
        for rank in RANKS:
            img = draw_clean_card(rank, suit_sym, color)
            path = os.path.join(OUTPUT_DIR, f"{RANK_CODE[rank]}{suit_code}.png")
            img.save(path)
            count += 1
            print(f"Generated {RANK_CODE[rank]}{suit_code}.png")

    back = draw_joker_back()
    back.save(os.path.join(OUTPUT_DIR, "back.png"))
    print("Generated back.png")
    
if __name__ == "__main__":
    main()
