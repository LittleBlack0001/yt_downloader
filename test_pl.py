from pytubefix import Playlist

# 直接測試你的公開清單
url = "https://www.youtube.com/playlist?list=PLKaJjDQANDJE"
pl = Playlist(url)

print(f"清單標題: {pl.title}")
print(f"抓到的影片數量: {len(pl.videos)}")