"""
把 cards/ 裡的 PNG 複製並重新命名成 Discord Emoji 格式
原始命名：AS.png / 0H.png / KD.png
目標命名：sp_A.png / cu_10.png / lo_K.png

sp=♠️  cu=♥️  lo=♦️  pb=♣️
"""
import os, shutil

SRC = "cards"
DST = "emoji_cards"

SUIT_MAP = {'S': 'sp', 'H': 'cu', 'D': 'lo', 'C': 'pb'}
RANK_MAP = {'0': '10', 'A': 'A', '2': '2', '3': '3', '4': '4',
            '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
            'J': 'J', 'Q': 'Q', 'K': 'K'}

os.makedirs(DST, exist_ok=True)

converted = 0
for fname in os.listdir(SRC):
    if not fname.endswith('.png'):
        continue
    
    base = fname[:-4]  # 去掉 .png
    
    if base == 'back':
        shutil.copy(os.path.join(SRC, fname), os.path.join(DST, 'card_back.png'))
        print(f"  back.png  ->  card_back.png")
        converted += 1
        continue
    
    if len(base) < 2:
        continue
    
    # 解析：最後一個字元是花色，前面是點數
    suit_code = base[-1]     # S / H / D / C
    rank_code = base[:-1]    # A / 2~9 / 0 / J / Q / K
    
    suit = SUIT_MAP.get(suit_code)
    rank = RANK_MAP.get(rank_code)
    
    if not suit or not rank:
        print(f"  [SKIP] {fname}")
        continue
    
    new_name = f"{suit}_{rank}.png"
    shutil.copy(os.path.join(SRC, fname), os.path.join(DST, new_name))
    print(f"  {fname:10s}  ->  {new_name}")
    converted += 1

print(f"\n[DONE] Converted {converted} files -> ./{DST}/")
print(f"\nDiscord Emoji upload checklist:")
print(f"  - Go to Server Settings > Emoji > Upload Emoji")
print(f"  - Upload all PNG files from ./{DST}/")
print(f"  - Emoji names will auto-fill from filename (e.g. sp_A, cu_10, etc.)")
