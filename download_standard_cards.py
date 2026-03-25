import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OUTPUT_DIR = "cards"
os.makedirs(OUTPUT_DIR, exist_ok=True)

SUITS = ['S', 'H', 'D', 'C']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'J', 'Q', 'K']

# 生成所有卡牌的代碼 (跟 deckofcardsapi 相符)
codes = [r + s for r in RANKS for s in SUITS] + ['back']

def download_card(code):
    url = f"https://deckofcardsapi.com/static/img/{code}.png"
    out_path = os.path.join(OUTPUT_DIR, f"{code}.png")
    
    # 設置 User-Agent 避免 403 Forbidden
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            with open(out_path, 'wb') as f:
                f.write(response.read())
        print(f"  [OK] Downloaded {code}.png")
    except Exception as e:
        print(f"  [ERROR] Failed to download {code}.png: {e}")

def main():
    print(f"Downloading 53 standard playing cards from deckofcardsapi to ./{OUTPUT_DIR}/...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        executor.map(download_card, codes)
    print("\n[DONE] All standard cards downloaded successfully!")

if __name__ == "__main__":
    main()
