"""
撲克牌 PNG 生成器
生成 52 張牌 + 牌背，命名規則：AS.png, 2H.png, 0D.png(10), KS.png, back.png
需要: pip install Pillow
"""
from PIL import Image, ImageDraw, ImageFont
import os

# ── 設定 ─────────────────────────────────────────
OUTPUT_DIR = "cards"
W, H = 200, 280   # 卡牌尺寸 (像素)
RADIUS = 14       # 圓角半徑

SUITS = [
    # (花色符號, 代碼, 顏色)
    ('♠', 'S', (30,  41,  59)),   # 黑桃 — 深藍黑
    ('♥', 'H', (220, 38,  38)),   # 紅心 — 紅
    ('♦', 'D', (220, 38,  38)),   # 方塊 — 紅
    ('♣', 'C', (30,  41,  59)),   # 梅花 — 深藍黑
]

RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
RANK_CODE = {r: r for r in RANKS}
RANK_CODE['10'] = '0'   # 10 用 0 表示 (跟 deckofcardsapi 相同)

# ── 字型載入 ──────────────────────────────────────
def load_font(size, bold=False):
    candidates = [
        ("arialbd.ttf" if bold else "arial.ttf"),
        (r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"),
        (r"C:\Windows\Fonts\calibrib.ttf" if bold else r"C:\Windows\Fonts\calibri.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

FONT_RANK    = load_font(26, bold=True)
FONT_SUIT_SM = load_font(18)
FONT_SUIT_LG = load_font(72)

# ── 繪製單張牌 ────────────────────────────────────
def draw_card(rank: str, suit_sym: str, color: tuple) -> Image.Image:
    img  = Image.new('RGB', (W, H), '#F8FAFC')      # 淡灰白底
    draw = ImageDraw.Draw(img)

    # 圓角外框
    draw.rounded_rectangle([1, 1, W-2, H-2], radius=RADIUS,
                           fill='white', outline='#CBD5E1', width=2)

    # ── 左上角 ──
    draw.text((12, 8),  rank,     font=FONT_RANK,    fill=color)
    draw.text((14, 37), suit_sym, font=FONT_SUIT_SM, fill=color)

    # ── 中央大花色 ──
    draw.text((W // 2, H // 2), suit_sym, font=FONT_SUIT_LG,
              fill=color, anchor='mm')

    # ── 右下角 (旋轉 180°) ──
    tmp = Image.new('RGB', (44, 56), 'white')
    td  = ImageDraw.Draw(tmp)
    td.text((2, 2),  rank,     font=FONT_RANK,    fill=color)
    td.text((4, 30), suit_sym, font=FONT_SUIT_SM, fill=color)
    rotated = tmp.rotate(180)
    img.paste(rotated, (W - 44 - 10, H - 56 - 8))

    return img

# ── 繪製牌背 ─────────────────────────────────────
def draw_back() -> Image.Image:
    img  = Image.new('RGB', (W, H), '#F8FAFC')
    draw = ImageDraw.Draw(img)

    # 外框
    draw.rounded_rectangle([1, 1, W-2, H-2], radius=RADIUS,
                           fill='white', outline='#CBD5E1', width=2)
    # 深色背景
    draw.rounded_rectangle([8, 8, W-8, H-8], radius=10,
                           fill='#1e3a5f')
    # 裝飾格紋
    for x in range(8, W-8, 16):
        draw.line([(x, 8), (x, H-8)], fill='#1a3352', width=1)
    for y in range(8, H-8, 16):
        draw.line([(8, y), (W-8, y)], fill='#1a3352', width=1)
    # 中央菱形
    cx, cy = W // 2, H // 2
    r = 30
    draw.polygon([(cx, cy-r), (cx+r, cy), (cx, cy+r), (cx-r, cy)],
                 fill='#e11d48', outline='white')
    return img

# ── 主程式 ────────────────────────────────────────
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    count = 0

    for suit_sym, suit_code, color in SUITS:
        for rank in RANKS:
            filename = f"{RANK_CODE[rank]}{suit_code}.png"
            path = os.path.join(OUTPUT_DIR, filename)
            img = draw_card(rank, suit_sym, color)
            img.save(path)
            count += 1
            print(f"  [OK] {filename}")

    # 牌背
    back = draw_back()
    back.save(os.path.join(OUTPUT_DIR, "back.png"))
    print(f"  [OK] back.png")
    count += 1

    print(f"\n[DONE] Generated {count} card images in ./{OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
