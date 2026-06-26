import os
import re
import uuid
import queue
import threading
import subprocess
import json
import shutil
import urllib.parse
import time
from flask import Flask, render_template, request, jsonify, Response, send_file
from pytubefix import YouTube, Playlist

app = Flask(__name__)

TEMP_DIR = os.path.join(os.path.dirname(__file__), 'temp')
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

# ==========================================
# 步驟一：分析影片與清單資訊
# ==========================================
@app.route('/api/get_info', methods=['POST'])
def get_info():
    try:
        data = request.get_json()
        video_url = data.get('url')

        if not video_url:
            return jsonify({"success": False, "error": "請提供有效的網址"})

        is_playlist = 'list=' in video_url
        playlist_info = None

        if is_playlist:
            try:
                # 提取 list ID，強制轉換成乾淨的清單專屬網址
                parsed_url = urllib.parse.urlparse(video_url)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                
                if 'list' in query_params:
                    playlist_id = query_params['list'][0]
                    clean_pl_url = f"https://www.youtube.com/playlist?list={playlist_id}"
                    
                    # 使用 client='TV' 偽裝身份繞過驗證
                    pl = Playlist(clean_pl_url, client='TV')
                    
                    try:
                        length = len(list(pl.video_urls))
                    except:
                        length = 0

                    playlist_info = {
                        "title": pl.title if pl.title else "未知合輯",
                        "length": length, 
                        "url": clean_pl_url
                    }
            except Exception as e:
                print(f"解析清單失敗，視為單曲處理: {e}")

        try:
             # 使用 client='TV' 偽裝身份繞過驗證
             yt = YouTube(video_url, client='TV')
             title = yt.title
             
             v_streams = yt.streams.filter(type="video", adaptive=True)
             resolutions = list(set([s.resolution for s in v_streams if s.resolution]))
             resolutions.sort(key=lambda x: int(x[:-1]) if x[:-1].isdigit() else 0, reverse=True)

             a_streams = yt.streams.filter(type="audio")
             audios = list(set([s.abr for s in a_streams if s.abr]))
             audios.sort(key=lambda x: int(x.replace('kbps', '')) if x.replace('kbps', '').isdigit() else 0, reverse=True)
             
             single_video_info = {
                 "title": title,
                 "resolutions": resolutions,
                 "audios": audios,
                 "url": video_url
             }
        except Exception as e:
             if not playlist_info:
                 raise Exception(f"無法解析影片。錯誤: {str(e)}")
             else:
                 single_video_info = None

        return jsonify({
            "success": True,
            "is_playlist": is_playlist and playlist_info is not None,
            "playlist_info": playlist_info,
            "single_video_info": single_video_info
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ==========================================
# 內部核心：單曲與合輯的下載處理引擎
# ==========================================
def process_single_video(url, res, audio, output_dir, file_prefix, q, base_progress=0, weight=100):
    def create_progress_callback(step_base, step_weight, step_name, title):
        def on_progress(stream, chunk, bytes_remaining):
            try:
                total = stream.filesize
                if not total: total = 1 
                downloaded = total - bytes_remaining
                current_p = step_base + int((downloaded / total) * step_weight)
                q.put({
                    "status": "downloading", 
                    "progress": current_p, 
                    "msg": f"{step_name}: {title[:15]}... ({current_p}%)"
                })
            except Exception:
                pass 
        return on_progress

    # 使用 client='TV' 偽裝身份繞過驗證
    yt_info = YouTube(url, client='TV')
    safe_title = re.sub(r'[\\/*?:"<>|]', "", yt_info.title) if yt_info.title else "未知影片"
    
    v_weight = weight * 0.45
    a_weight = weight * 0.45

    # 1. 影像下載
    yt_v = YouTube(url, client='TV', on_progress_callback=create_progress_callback(base_progress, v_weight, "影像下載", safe_title))
    v_stream = yt_v.streams.filter(type="video", resolution=res, adaptive=True).first()
    if not v_stream:
         v_stream = yt_v.streams.filter(type="video", adaptive=True).order_by('resolution').desc().first()
    if not v_stream:
         raise Exception("無法取得影像串流")
    v_file = v_stream.download(output_path=output_dir, filename=f"{file_prefix}_video.mp4")

    # 2. 音訊下載
    audio_base = base_progress + int(v_weight)
    yt_a = YouTube(url, client='TV', on_progress_callback=create_progress_callback(audio_base, a_weight, "音訊下載", safe_title))
    a_stream = yt_a.streams.filter(type="audio", abr=audio).first()
    if not a_stream:
         a_stream = yt_a.streams.filter(type="audio").order_by('abr').desc().first()
    if not a_stream:
         raise Exception("無法取得音訊串流")
    a_file = a_stream.download(output_path=output_dir, filename=f"{file_prefix}_audio.mp4")

    # 3. 合併
    merge_base = audio_base + int(a_weight)
    q.put({"status": "downloading", "progress": merge_base, "msg": f"影音縫合中: {safe_title[:15]}... ({merge_base}%)"})
    
    final_filename = f"{safe_title}.mp4"
    output_file_path = os.path.join(output_dir, final_filename)
    
    cmd = f'ffmpeg -y -i "{v_file}" -i "{a_file}" -c:v copy -c:a aac "{output_file_path}"'
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(v_file): os.remove(v_file)
    if os.path.exists(a_file): os.remove(a_file)
    
    if not os.path.exists(output_file_path):
        raise Exception("合併失敗！請確認電腦環境已正確安裝 ffmpeg。")
        
    return final_filename

def download_worker(url, res, audio, task_id, mode, q):
    try:
        if mode == 'playlist':
            # 這裡的 Playlist 物件主要用來取得基本資訊，若需解析網址清單，前面 get_info 已處理大部分
            pl = Playlist(url, client='TV')
            safe_pl_title = re.sub(r'[\\/*?:"<>|]', "", pl.title) if pl.title else "未知合輯"
            
            video_urls = list(pl.video_urls)
            total_videos = len(video_urls)
            
            if total_videos == 0:
                raise Exception("抓取不到影片！請確認清單為「公開」。")

            playlist_dir = os.path.join(TEMP_DIR, task_id)
            os.makedirs(playlist_dir, exist_ok=True)
            
            weight_per_video = 95 / total_videos
            successful_count = 0
            
            for index, video_url in enumerate(video_urls):
                base_p = int(index * weight_per_video)
                try:
                    process_single_video(video_url, res, audio, playlist_dir, f"{task_id}_{index}", q, base_progress=base_p, weight=weight_per_video)
                    successful_count += 1
                except Exception as ve:
                    error_msg = f"第 {index+1} 首下載失敗: {ve}"
                    print(error_msg)
                    with open(os.path.join(playlist_dir, f"錯誤紀錄_第{index+1}首.txt"), 'w', encoding='utf-8') as f:
                        f.write(error_msg)

            if successful_count == 0:
                raise Exception("合輯內所有影片皆下載失敗！")

            q.put({"status": "merging", "progress": 98, "msg": "合輯下載完成，正在打包 ZIP... (98%)"})
            zip_path = os.path.join(TEMP_DIR, task_id)
            shutil.make_archive(zip_path, 'zip', playlist_dir)
            shutil.rmtree(playlist_dir)

            q.put({
                "status": "complete", "progress": 100, 
                "msg": "打包完成！即將觸發下載 (100%)",
                "task_id": task_id, "filename": f"{safe_pl_title}.zip", "is_zip": True
            })

        else:
            q.put({"status": "downloading", "progress": 1, "msg": "準備下載單曲... (1%)"})
            final_name = process_single_video(url, res, audio, TEMP_DIR, task_id, q, base_progress=0, weight=100)
            
            temp_final_path = os.path.join(TEMP_DIR, final_name)
            system_final_path = os.path.join(TEMP_DIR, f"{task_id}_final.mp4")
            os.rename(temp_final_path, system_final_path)

            q.put({
                "status": "complete", "progress": 100, 
                "msg": "打包完成！即將觸發下載 (100%)",
                "task_id": task_id, "filename": final_name, "is_zip": False
            })

    except Exception as e:
        q.put({"status": "error", "msg": f"執行失敗: {str(e)}"})

# ==========================================
# 步驟二：即時進度通訊
# ==========================================
@app.route('/api/download_stream')
def download_stream():
    url = request.args.get('url')
    res = request.args.get('res')
    audio = request.args.get('audio')
    mode = request.args.get('mode', 'single')
    
    task_id = str(uuid.uuid4())
    q = queue.Queue()

    threading.Thread(target=download_worker, args=(url, res, audio, task_id, mode, q)).start()

    def event_generator():
        while True:
            try:
                data = q.get(timeout=60)
                yield f"data: {json.dumps(data)}\n\n"
                if data['status'] in ['complete', 'error']:
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'status': 'error', 'msg': '伺服器處理逾時或過載'})}\n\n"
                break

    return Response(event_generator(), mimetype='text/event-stream')

# ==========================================
# 背景清理函式：延遲刪除檔案
# ==========================================
def delete_file_delayed(file_path, delay=60):
    time.sleep(delay)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"🧹 已自動清理暫存檔案: {file_path}")
    except Exception as e:
        print(f"清理檔案失敗: {e}")

# ==========================================
# 步驟三：回傳最終檔案
# ==========================================
@app.route('/api/fetch_file')
def fetch_file():
    task_id = request.args.get('task_id')
    filename = request.args.get('filename')
    is_zip = request.args.get('is_zip') == 'true'
    
    if is_zip:
        file_path = os.path.join(TEMP_DIR, f"{task_id}.zip")
    else:
        file_path = os.path.join(TEMP_DIR, f"{task_id}_final.mp4")
    
    if os.path.exists(file_path):
        threading.Thread(target=delete_file_delayed, args=(file_path, 60)).start()
        return send_file(file_path, as_attachment=True, download_name=filename)
    else:
        return "檔案不存在或已被刪除", 404

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5000)