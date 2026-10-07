# HVOS PC VR (Base Edition) v0.7 / 2026-10-07  ※Meta Quest 3 専用
# API キーは config.json から読みます（初回起動時に入力）。
#
# 0.1 MQ【公開】260908 / 0.2 MQ→PC【修正】260908 / 0.3 PC【追加・修正】260911
# 0.5 PC→MQ【高速化対応】260913
# 0.7 【API キーを config.json 方式に変更 / モデルを二択に（既定: gemini-3.5-flash-lite）
#      / 画像を縮小して送信 / 撮影画像の自動保存を廃止 / ログに [画面] [API] の印】
import base64
import io
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime

import keyboard
import pyautogui
import pygetwindow as gw
from flask import Flask, jsonify, render_template_string, request
from google import genai
from PIL import Image

# ==========================================
# 0. コンソール出力のUTF-8化（ASCIIエラー対策）
# ==========================================
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

# プログラムと同じフォルダに設定ファイルを置く（管理者実行でも場所がずれない）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")  # APIキー・選択モデル（他人に渡さない）

# ==========================================
# モデル設定（二択）
# ==========================================
AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",  # デフォルト（高速）
    "gemini-3.6-flash",       # 標準（混雑時に 503 が出ることがあります）
]
DEFAULT_MODEL = AVAILABLE_MODELS[0]

# 画像縮小の上限（長辺ピクセル）。トークン節約と高速化のため
MAX_IMAGE_SIDE = 1280


# ==========================================
# 🔑 設定の読み書き（config.json）
# ==========================================
def read_config():
    if os.path.exists(API_CONFIG_FILE):
        try:
            with open(API_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[API] config.json の読み込みに失敗しました: {e}")
    return {}


def write_config(api_key, model):
    try:
        with open(API_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'api_key': api_key, 'model': model}, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[API] config.json の保存に失敗しました: {e}")
        return False


def load_settings():
    """config.json からキーとモデルを読む。キーが無ければ入力を求めて保存する。"""
    cfg = read_config()
    key = str(cfg.get('api_key', '')).strip()
    model = cfg.get('model')
    if model not in AVAILABLE_MODELS:
        model = DEFAULT_MODEL

    if not key:
        print("==========================================")
        print(" 初回設定: Gemini API キーを貼り付けて Enter を押してください")
        print(" （黒い画面では、右クリックで貼り付けできます）")
        print("==========================================")
        key = input("APIキー: ").strip().strip('"').strip("'").strip()
        if not key:
            print("[API] キーが入力されませんでした。終了します。")
            input("Enter キーで閉じます")
            sys.exit(1)
        if write_config(key, model):
            print("[API] config.json に保存しました。次回から入力は不要です。")
            print("[API] キーを変えたいときは config.json を削除して、もう一度起動してください。")
    return key, model


GEMINI_API_KEY, selected_model = load_settings()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = Flask(__name__)

latest_result = {
    "title": "HVOS PC VR (v0.7) - スタンバイ完了",
    "gemini_content": "【HVOS (PC VR v0.7) スタンバイOK！】\nキーボードの【1】を押すと、撮影＆解析を実行します。",
    "timestamp": ""
}

is_processing = False
processing_lock = threading.Lock()


def get_local_ip():
    """Quest 3 のブラウザからアクセスするためのローカルIPアドレスを取得"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def shrink_image(img):
    """長辺が MAX_IMAGE_SIDE を超える場合だけ縮小する（トークン節約・高速化）"""
    w, h = img.size
    longest = max(w, h)
    if longest <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / longest
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def bring_meta_cast_to_front():
    try:
        all_wins = gw.getAllWindows()
        target_win = None
        for win in all_wins:
            title_lower = win.title.lower()
            if any(k in title_lower for k in ["meta casting", "casting", "oculus", "meta"]):
                target_win = win
                break

        if target_win:
            if target_win.isMinimized:
                target_win.restore()
                time.sleep(0.05)
            pyautogui.press('alt')
            target_win.activate()
            time.sleep(0.1)
            return target_win
    except Exception as e:
        print(f"[画面] ウィンドウ制御エラー: {e}")
    return None


def execute_analysis():
    global is_processing

    with processing_lock:
        if is_processing:
            print("[画面] 解析処理中のためスキップします。")
            return
        is_processing = True

    model_name = selected_model  # 処理開始時点で確定させる
    latest_result["title"] = f"解析中... ({model_name})"
    latest_result["gemini_content"] = "画像を分析しています...数秒お待ちください。"
    latest_result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        print("[画面] キャプチャ実行")
        cast_win = bring_meta_cast_to_front()

        # 領域指定キャプチャによる高速化
        if cast_win and cast_win.width > 0 and cast_win.height > 0:
            left, top = max(0, cast_win.left), max(0, cast_win.top)
            width, height = cast_win.width, cast_win.height
            # Meta Questのキャスト領域切りだし
            crop_left = left + int(width * 0.02)
            crop_top = top + int(height * 0.08)
            crop_width = int(width * 0.96)
            crop_height = int(height * 0.90)
            captured = pyautogui.screenshot(region=(crop_left, crop_top, crop_width, crop_height))
        else:
            print("[画面] キャストウィンドウが見つからないため、画面全体から切り出します。")
            screenshot = pyautogui.screenshot()
            w, h = screenshot.size
            captured = screenshot.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95)))

        # 縮小してメモリ上で渡す（ディスクには保存しない）
        img = shrink_image(captured)
        print(f"[画面] 送信サイズ: {img.size[0]}x{img.size[1]}")

        prompt = (
            "あなたは最高のバーチャルツアーガイドです。このVR画像に写っている景色・場所について、"
            "以下の構成で500文字程度で魅力的に解説してください。\n\n"
            "1. 【場所の特定と概要】：ここがどこか、何という施設・景色か\n"
            "2. 【歴史と背景】：この場所にまつわる深い歴史やストーリー、建築のこだわりなど\n"
            "3. 【ここだけの魅力・おすすめポイント】：訪れた人が『ワクワクする』豆知識や見どころ\n"
            "4. 【周囲のおすすめ・楽しみ方】：もし実際にここを歩くなら立ち寄るべき周辺スポットや楽しみ方\n\n"
            "語り口は親しみやすく、聞いているだけで旅に出たくなるようなワクワクする文章でまとめてください。"
            "文末には必ず『（文字数：〇〇文字）』と実際に生成した文字数を記載してください。"
        )

        print(f"[API] 呼び出し実行モデル: {model_name}")
        response = gemini_client.models.generate_content(
            model=model_name,
            contents=[prompt, img]
        )

        latest_result["title"] = f"HVOS ガイド解説 ({model_name})"
        latest_result["gemini_content"] = response.text
        print("[API] 解析完了")

    except Exception as e:
        print(f"[API] エラー: {e}")
        latest_result["title"] = f"エラー発生 ({model_name})"
        latest_result["gemini_content"] = f"処理エラー: {e}"

    finally:
        with processing_lock:
            is_processing = False


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: sans-serif; padding: 15px; margin: 0; }
        .header { font-size: 1rem; font-weight: bold; color: #818cf8; border-bottom: 1px solid #334155; padding-bottom: 6px; display: flex; justify-content: space-between; }
        .header select { background-color: #2d3748; color: #ffffff; border: 1px solid #475569; border-radius: 4px; padding: 1px 4px; font-size: 0.7rem; }
        .card { background: #1e293b; border-radius: 8px; padding: 15px; margin-top: 12px; border-top: 4px solid #38bdf8; }
        .content { font-size: 0.9rem; line-height: 1.6; white-space: pre-wrap; }
    </style>
    <script>
        setInterval(async () => {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                document.getElementById('ui-title').innerText = data.title;
                document.getElementById('ui-timestamp').innerText = data.timestamp;
                document.getElementById('ui-gemini').innerText = data.gemini_content;
                var sel = document.getElementById('model');
                if (document.activeElement !== sel && sel.value !== data.model) {
                    sel.value = data.model;
                }
            } catch (e) {}
        }, 500);

        function changeModel(value) {
            fetch('/api/model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: value })
            });
        }
    </script>
</head>
<body>
    <div class="header">
        <span id="ui-title">{{ title }}</span>
        <span style="display:flex; align-items:center; gap:8px;">
            <span id="ui-timestamp" style="font-size:0.75rem; color:#64748b;">{{ timestamp }}</span>
            <select id="model" title="使用モデル" onchange="changeModel(this.value)">
                {% for m in models %}
                <option value="{{ m }}" {% if m == model %}selected{% endif %}>{{ m }}</option>
                {% endfor %}
            </select>
        </span>
    </div>
    <div class="card">
        <div id="ui-gemini" class="content">{{ gemini_content }}</div>
    </div>
</body>
</html>
"""


@app.route('/')
def home():
    return render_template_string(
        HTML_TEMPLATE,
        title=latest_result["title"],
        timestamp=latest_result["timestamp"],
        gemini_content=latest_result["gemini_content"],
        models=AVAILABLE_MODELS,
        model=selected_model,
    )


@app.route('/api/data')
def get_data():
    data = dict(latest_result)
    data["model"] = selected_model
    return jsonify(data)


@app.route('/api/model', methods=['POST'])
def set_model():
    """ブラウザのドロップダウンからモデルを切り替える（候補にあるものだけ受け付ける）"""
    global selected_model
    payload = request.get_json(silent=True) or {}
    new_model = payload.get("model")
    if new_model not in AVAILABLE_MODELS:
        return jsonify({"ok": False, "error": "unknown model"}), 400
    selected_model = new_model
    write_config(GEMINI_API_KEY, selected_model)
    print(f"[API] 使用モデル変更 ➔ 【{selected_model}】")
    return jsonify({"ok": True, "model": selected_model})


def start_keyboard_listener():
    print("[画面] 【1】キーの監視を開始しました。")
    last_execution_time = 0
    cooldown_seconds = 3.0  # 連打防止のクールダウン

    while True:
        if keyboard.is_pressed("1") or keyboard.is_pressed("num 1"):
            current_time = time.time()
            if current_time - last_execution_time > cooldown_seconds:
                last_execution_time = current_time
                if not is_processing:
                    threading.Thread(target=execute_analysis, daemon=True).start()

            while keyboard.is_pressed("1") or keyboard.is_pressed("num 1"):
                time.sleep(0.02)

        time.sleep(0.02)


if __name__ == '__main__':
    threading.Thread(target=start_keyboard_listener, daemon=True).start()

    local_ip = get_local_ip()

    print("\n==========================================")
    print("=== HVOS PC VR Base Edition v0.7 稼働中 ===")
    print(f"・Questブラウザ用URL: http://{local_ip}:5000")
    print(f"・使用モデル        : {selected_model}")
    print("==========================================\n")

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
