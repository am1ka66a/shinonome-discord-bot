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
FONT_SUIT_LG = load_font(60)   # 用於 A/J/Q/K 中央大符號
FONT_PIP_SM  = load_font(22)   # 用於數字牌的花色點

# ── 數字牌花色點位置（單位：相對 W/H 的比例）──────────
# 每個 rank 對應的 (x%, y%) 列表
PIP_POSITIONS = {
    'A': [(0.5, 0.5)],
    '2': [(0.5, 0.28), (0.5, 0.72)],
    '3': [(0.5, 0.28), (0.5, 0.50), (0.5, 0.72)],
    '4': [(0.30, 0.28), (0.70, 0.28),
          (0.30, 0.72), (0.70, 0.72)],
    '5': [(0.30, 0.28), (0.70, 0.28),
          (0.5,  0.50),
          (0.30, 0.72), (0.70, 0.72)],
    '6': [(0.30, 0.28), (0.70, 0.28),
          (0.30, 0.50), (0.70, 0.50),
          (0.30, 0.72), (0.70, 0.72)],
    '7': [(0.30, 0.25), (0.70, 0.25),
          (0.5,  0.38),
          (0.30, 0.50), (0.70, 0.50),
          (0.30, 0.72), (0.70, 0.72)],
    '8': [(0.30, 0.25), (0.70, 0.25),
          (0.5,  0.37),
          (0.30, 0.50), (0.70, 0.50),
          (0.5,  0.63),
          (0.30, 0.73), (0.70, 0.73)],
    '9': [(0.30, 0.22), (0.70, 0.22),
          (0.30, 0.38), (0.70, 0.38),
          (0.5,  0.50),
          (0.30, 0.62), (0.70, 0.62),
          (0.30, 0.78), (0.70, 0.78)],
    '10':[(0.30, 0.22), (0.70, 0.22),
          (0.5,  0.30),
          (0.30, 0.40), (0.70, 0.40),
          (0.30, 0.60), (0.70, 0.60),
          (0.5,  0.70),
          (0.30, 0.78), (0.70, 0.78)],
}

def draw_pip(draw, cx, cy, suit_sym, color, font):
    """在 (cx, cy) 畫一個花色符號，以中心對齊"""
    draw.text((cx, cy), suit_sym, font=font, fill=color, anchor='mm')

# ── 繪製數字牌（A/2~10）────────────────────────────
def draw_number_card(rank: str, suit_sym: str, color: tuple) -> Image.Image:
    img  = Image.new('RGB', (W, H), '#F8FAFC')
    draw = ImageDraw.Draw(img)

    # 圓角框
    draw.rounded_rectangle([1, 1, W-2, H-2], radius=RADIUS,
                           fill='white', outline='#CBD5E1', width=2)

    # 左上角：點數 + 花色
    draw.text((12, 8),  rank,     font=FONT_RANK,    fill=color)
    draw.text((14, 37), suit_sym, font=FONT_SUIT_SM, fill=color)

    # 右下角（旋轉180°）
    tmp = Image.new('RGB', (44, 56), 'white')
    td  = ImageDraw.Draw(tmp)
    td.text((2, 2),  rank,     font=FONT_RANK,    fill=color)
    td.text((4, 30), suit_sym, font=FONT_SUIT_SM, fill=color)
    rotated = tmp.rotate(180)
    img.paste(rotated, (W - 44 - 10, H - 56 - 8))

    # 中央花色點陣
    pip_font_size = 36 if rank == 'A' else 22
    pip_font = load_font(pip_font_size)
    positions = PIP_POSITIONS.get(rank, [(0.5, 0.5)])
    for (xr, yr) in positions:
        cx = int(W * xr)
        cy = int(H * yr)
        draw.text((cx, cy), suit_sym, font=pip_font, fill=color, anchor='mm')

    return img

# ── 繪製人臉牌（J/Q/K）────────────────────────────
FACE_LABELS = {'J': 'J', 'Q': 'Q', 'K': 'K'}
FACE_BG = {
    'J': '#FEF9C3',   # 淡黃
    'Q': '#FCE7F3',   # 淡粉
    'K': '#EDE9FE',   # 淡紫
}

def draw_face_card(rank: str, suit_sym: str, color: tuple) -> Image.Image:
    bg = FACE_BG.get(rank, '#FFFFFF')
    img  = Image.new('RGB', (W, H), '#F8FAFC')
    draw = ImageDraw.Draw(img)

    # 外框
    draw.rounded_rectangle([1, 1, W-2, H-2], radius=RADIUS,
                           fill=bg, outline='#CBD5E1', width=2)
    # 內框裝飾線
    draw.rounded_rectangle([10, 10, W-10, H-10], radius=10,
                           fill=None, outline=color, width=1)

    # 左上角
    draw.text((12, 8),  rank,     font=FONT_RANK,    fill=color)
    draw.text((14, 37), suit_sym, font=FONT_SUIT_SM, fill=color)

    # 右下角
    tmp = Image.new('RGB', (44, 56), bg)
    td  = ImageDraw.Draw(tmp)
    td.text((2, 2),  rank,     font=FONT_RANK,    fill=color)
    td.text((4, 30), suit_sym, font=FONT_SUIT_SM, fill=color)
    rotated = tmp.rotate(180)
    img.paste(rotated, (W - 44 - 10, H - 56 - 8))

    # 中央：大點數字母
    font_center = load_font(80, bold=True)
    draw.text((W // 2, H // 2 - 16), rank, font=font_center,
              fill=color, anchor='mm')
    # 中央下方：花色
    draw.text((W // 2, H // 2 + 40), suit_sym, font=FONT_SUIT_LG,
              fill=color, anchor='mm')

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

    face_ranks = {'J', 'Q', 'K'}

    for suit_sym, suit_code, color in SUITS:
        for rank in RANKS:
            filename = f"{RANK_CODE[rank]}{suit_code}.png"
            path = os.path.join(OUTPUT_DIR, filename)

            if rank in face_ranks:
                img = draw_face_card(rank, suit_sym, color)
            else:
                img = draw_number_card(rank, suit_sym, color)

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
