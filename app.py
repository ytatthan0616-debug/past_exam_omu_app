import streamlit as st
import json
import random
import os
import re
import datetime
import plotly.graph_objects as go
import google.generativeai as genai
from PIL import Image
import time
import hashlib

# --- ここから追加：Firebaseの準備 ---
import firebase_admin
from firebase_admin import credentials, db

# すでに起動しているか確認してから初期化する
if not firebase_admin._apps:
    key_dict = json.loads(st.secrets["FIREBASE_JSON"])
    cred = credentials.Certificate(key_dict)
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://past-exam-app-266ac-default-rtdb.asia-southeast1.firebasedatabase.app/'
    })
# --- クラウドデータベース用の読み書き関数 (Realtime DB版) ---

@st.cache_data
def load_data():
    ref = db.reference('app_data/all_questions')
    data = ref.get()
    return data if data else {}

@st.cache_data
def load_genre_data(genre):
    """指定された分野（ジャンル）の問題だけをピンポイントでダウンロードする"""
    # 💡 ポイント：all_questions のさらに下の「分野名」まで直接パスを指定する
    ref = db.reference(f'app_data/all_questions/{genre}')
    genre_data = ref.get()
    
    return genre_data if genre_data else []

def save_data(data_dict):
    ref = db.reference('app_data/all_questions')
    ref.set(data_dict)
    st.cache_data.clear()

@st.cache_data
def load_evals(username):  # 💡 ここに username を追加
    # （st.session_state... の行は削除して、引数の username を直接使います）
    ref = db.reference(f'users/{username}/evals')
    data = ref.get()
    if not data: return {}
    
    # Firebase用の安全なキー（全角）を、アプリ用の半角に戻す
    res = {}
    for g, qs in data.items():
        res[g] = {}
        for k, v in qs.items():
            orig_k = k.replace('．', '.').replace('＃', '#')
            res[g][orig_k] = v
    return res

def save_evals(evals):
    username = st.session_state.get("username", "Guest")
    ref = db.reference(f'users/{username}/evals')
    
    # Firebaseで禁止されている半角記号を全角に変換して保存
    safe_evals = {}
    for g, qs in evals.items():
        safe_evals[g] = {}
        for k, v in qs.items():
            safe_k = k.replace('.', '．').replace('#', '＃')
            safe_evals[g][safe_k] = v
    ref.set(safe_evals)
    st.cache_data.clear()

# （既存の save_evals の下に追加します）

def update_single_eval(genre, q_key, single_eval_data):
    """1問の成績データだけをピンポイントでFirebaseに保存（通信量節約）"""
    username = st.session_state.get("username", "Guest")
    
    # Firebaseで禁止されている半角記号を全角に変換
    safe_k = q_key.replace('.', '．').replace('#', '＃')
    
    # 💡 ポイント：大元の evals フォルダではなく、その問題の住所を直接指定する
    ref = db.reference(f'users/{username}/evals/{genre}/{safe_k}')
    
    # その問題のデータだけを上書き
    ref.set(single_eval_data)
    
    # 画面に最新状態を反映させるためキャッシュをリセット
    st.cache_data.clear()

def load_config():
    username = st.session_state.get("username", "Guest")
    ref = db.reference(f'users/{username}/config')
    data = ref.get()
    default_conf = {
        "target_date": f"{datetime.date.today().year + 1}-08-20", 
        "exam_name": "大阪公立大学大学院 院試",
        "startup_mode": "ホーム",
        "font_size": "標準",
        "font_family": "標準 (ゴシック体)",
        "image_width": 700,
        "show_balloons": True,
        "gemini_api_key": "",
        "toeic_score": 0
    }
    return data if data else default_conf

def save_config(conf):
    username = st.session_state.get("username", "Guest")
    ref = db.reference(f'users/{username}/config')
    ref.set(conf)

# 💡 修正：通信量爆発の最大の原因！難易度計算のデータを1時間キャッシュする
@st.cache_data(ttl=3600) 
def get_global_q_stats():
    users_data = db.reference('users').get()
    q_stats = {}
    if not users_data: return q_stats
    
    for uname, udata in users_data.items():
        evals = udata.get('evals', {})
        for g, qs in evals.items():
            for k, val in qs.items():
                orig_k = k.replace('．', '.').replace('＃', '#')
                
                # 💡 修正：ジャンル名を含めた「完全な一意のキー」を作成して混線を防ぐ！
                unique_k = f"{g}_{orig_k}"
                
                rating = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                
                if rating in ["〇", "△", "▲", "×"]:
                    if unique_k not in q_stats:
                        q_stats[unique_k] = {"ans": 0, "score": 0.0}
                    q_stats[unique_k]["ans"] += 1
                    
                    if rating == "〇": q_stats[unique_k]["score"] += 1.0
                    elif rating == "△": q_stats[unique_k]["score"] += 0.66
                    elif rating == "▲": q_stats[unique_k]["score"] += 0.33
    return q_stats
def get_difficulty_ui(q_key, q_stats):
    if q_key not in q_stats or q_stats[q_key]["ans"] == 0:
        return "<span style='color: #888; font-size: 0.6em; font-weight: normal;'>📊 難易度: データなし</span>"
    
    ans = q_stats[q_key]["ans"]
    score = q_stats[q_key]["score"]
    acc = score / ans
    
    if acc >= 0.8:
        label = "🟢 簡単"
        color = "#4CAF50"
    elif acc >= 0.5:
        label = "🟡 普通"
        color = "#FF9800"
    else:
        label = "🔴 難問"
        color = "#F44336"
        
    return f"<span style='color: {color}; font-size: 0.6em; border: 1px solid {color}; padding: 2px 10px; border-radius: 12px; font-weight: normal;'>📊 難易度: {label} (正答率 {acc*100:.0f}%)</span>"


# ==========================================
# 💡 ランキング ＆ スコア計算用の共通関数
# ==========================================
def calculate_advanced_expected_scores(data, evals):
    """試験の特性に合わせた高度な予想得点計算"""
    scores = {"数学": 0.0, "電磁気": 0.0, "電気回路": 0.0}
    rating_map = {"〇": 1.0, "△": 0.66, "▲": 0.33, "×": 0.0}

    # 数学の計算
    if "数学" in evals and "数学" in data:
        math_total = 0.0
        for q_num in ["1", "2", "3", "4"]:
            num_evals = []
            for k, val in evals["数学"].items():
                if str(k).endswith(q_num):
                    r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                    if r in rating_map: num_evals.append(rating_map[r])
            if num_evals:
                math_total += 25.0 * (sum(num_evals) / len(num_evals))
        scores["数学"] = round(math_total, 1)

    # 電磁気・電気回路の計算
    for genre in ["電磁気", "電気回路"]:
        if genre in evals and genre in data:
            tag_counts = {}
            for q in data[genre]:
                for t in q.get("tags", []):
                    tag_counts[t] = tag_counts.get(t, 0) + 1

            if not tag_counts:
                genre_total_score = 0.0
                genre_ans_count = 0
                for q_key, val in evals[genre].items():
                    r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                    if r in rating_map:
                        genre_total_score += rating_map[r]
                        genre_ans_count += 1
                if genre_ans_count > 0:
                    scores[genre] = round(100.0 * (genre_total_score / len(data[genre])), 1)
                continue

            tag_mastery_sum = {t: 0.0 for t in tag_counts}
            tag_mastery_cnt = {t: 0 for t in tag_counts}
            for q_key, val in evals[genre].items():
                r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                if r not in rating_map: continue
                ratio = rating_map[r]
                tags = val.get("tags", []) if isinstance(val, dict) else []
                if not tags:
                    for q in data[genre]:
                        if str(q.get('number', '')) in str(q_key) and str(q.get('year', '')) in str(q_key):
                            tags = q.get("tags", [])
                            break
                for t in tags:
                    for sub_t in t.replace("，", ",").split(","):
                        sub_t = sub_t.strip()
                        if sub_t in tag_mastery_sum:
                            tag_mastery_sum[sub_t] += ratio
                            tag_mastery_cnt[sub_t] += 1
            
            total_weight = 0
            weighted_score_sum = 0
            for t, count in tag_counts.items():
                weight = count 
                total_weight += weight
                mastery = tag_mastery_sum[t] / tag_mastery_cnt[t] if tag_mastery_cnt[t] > 0 else 0.0
                weighted_score_sum += weight * mastery
            if total_weight > 0:
                scores[genre] = round(100.0 * (weighted_score_sum / total_weight), 1)
    return scores

@st.cache_data(ttl=7200)
def get_ranking_data(_data):
    """全員の学習状況を10分間キャッシュして取得する"""
    users_data = db.reference('users').get()
    if not users_data: return []

    ranking_list = []
    for uname, udata in users_data.items():
        evals = udata.get('evals', {})
        config = udata.get('config', {})
        
        # 1. 解いた問題数をカウント
        solved_count = 0
        for g, qs in evals.items():
            for k, val in qs.items():
                r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                if r in ["〇", "△", "▲", "×"]:
                    solved_count += 1
        
        if solved_count == 0: continue # 0問の人は除外
        
        # 2. 予想スコアを計算
        scores = calculate_advanced_expected_scores(_data, evals)
        toeic = config.get("toeic_score", 0)
        english_score = 100.0 if toeic >= 800 else (toeic / 800.0) * 100.0
        total_400 = scores.get("電気回路", 0) + scores.get("電磁気", 0) + scores.get("数学", 0) + english_score
        
        ranking_list.append({
            "name": uname,
            "solved": solved_count,
            "score": total_400
        })
    return ranking_list


# --- 分野の並び順設定 ---
GENRE_ORDER = ["電気回路", "電磁気", "数学"]

def get_genre_idx(genre):
    if genre in GENRE_ORDER:
        return GENRE_ORDER.index(genre)
    return 999

def render_beautiful_tags(tags):
    if not tags:
        return
    html = '<div style="display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px;">'
    for t in tags:
        # 画像風のグラデーションと角丸デザイン
        html += f'<span style="background: linear-gradient(135deg, #8a2be2, #4b0082); color: white; padding: 5px 15px; border-radius: 20px; font-size: 14px; border: 1px solid #bda0cb; box-shadow: 0 2px 4px rgba(0,0,0,0.3);">{t}</span>'
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)

def extract_num(prob_str):
    m = re.search(r'\d+', str(prob_str))
    return int(m.group()) if m else 0


def extract_year_val(year_str):
    m = re.search(r'[\d\.]+', str(year_str))
    return float(m.group()) if m else 0

st.set_page_config(page_title="過去問演習アプリ", layout="wide")



if "username" not in st.session_state:
    st.markdown("<h1 style='text-align: center;'>🚪 過去問演習アプリへようこそ</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>あなたの成績を保存するために、名前とパスワードを入力してください。</p>", unsafe_allow_html=True)
    
    st.info("""
            **【注意事項】**
入試問題は受験準備や教育目的での使用に限ります。
それ以外の目的での転載や二次利用は禁止されていますのでご注意ください。
""")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            user_name = st.text_input("ニックネーム (他人と被らない名前にしてください)")
            password = st.text_input("パスワード (初めての方はここで設定されます)", type="password")
            
            submitted = st.form_submit_button("学習をスタート 🐈💨", use_container_width=True)
            if submitted:
                if user_name.strip() == "" or password.strip() == "":
                    st.error("名前とパスワードの両方を入力してください！")
                else:
                    uname = user_name.strip()
                    # パスワードをそのまま保存するのは危険なので、暗号化（ハッシュ化）する
                    hashed_pw = hashlib.sha256(password.encode()).hexdigest()
                    
                    user_ref = db.reference(f'users/{uname}/auth')
                    auth_data = user_ref.get()
                    
                    if auth_data is None:
                        # 新規登録
                        user_ref.set({"password": hashed_pw})
                        st.session_state.username = uname
                        st.rerun()
                    else:
                        # 既存ユーザーのログインチェック
                        if auth_data.get("password") == hashed_pw:
                            st.session_state.username = uname
                            st.rerun()
                        else:
                            st.error("パスワードが間違っています。")
    st.stop() # ログインするまで下のアプリ画面を表示しない

conf = load_config()

theme_mode = conf.get("theme_mode", "標準 (デフォルト)")

if theme_mode == "標準 (デフォルト)":
    final_css = ""  # 標準のときは追加デザインを適用しない
else:
    base_css = """
    /* 共通設定（ボタンの丸みなど） */
    div.stButton > button { border-radius: 25px !important; font-weight: bold !important; transition: all 0.3s ease; }
    div[data-baseweb="input"] > div { border-radius: 15px !important; }
    """

    light_css = """
    /* ☀️ ライトモード（お昼の猫カフェ） */
    .stApp { background: linear-gradient(135deg, #FFF9E6 0%, #FFE4E1 100%) !important; }
    p, h1, h2, h3, label, li { color: #4A3000 !important; }
    div.stButton > button { background-color: #FFB6C1 !important; color: #4A3000 !important; border: 2px solid #FF9EAA !important; }
    div.stButton > button:hover { background-color: #FF69B4 !important; color: white !important; transform: scale(1.05); }
    div[data-baseweb="input"] > div { border: 2px solid #FFB6C1 !important; background-color: white !important; }
    """

    dark_css = """
    /* 🌙 ダークモード（夜の猫カフェ） */
    .stApp { background: linear-gradient(135deg, #2C222B 0%, #1A151F 100%) !important; }
    p, h1, h2, h3, label, li { color: #FFE4E1 !important; }
    div.stButton > button { background-color: #8B5A65 !important; color: #FFE4E1 !important; border: 2px solid #A06E7A !important; }
    div.stButton > button:hover { background-color: #FF69B4 !important; color: white !important; transform: scale(1.05); }
    div[data-baseweb="input"] > div { border: 2px solid #8B5A65 !important; background-color: #3D2D3A !important; }
    """

    # 設定に合わせて適用するCSSを決定
    if theme_mode == "お昼の猫カフェ (ライト)":
        final_css = f"<style>{base_css}{light_css}</style>"
    elif theme_mode == "夜の猫カフェ (ダーク)":
        final_css = f"<style>{base_css}{dark_css}</style>"
    else:
        # 自動（システム設定に従う）の場合
        final_css = f"<style>{base_css} @media (prefers-color-scheme: light) {{ {light_css} }} @media (prefers-color-scheme: dark) {{ {dark_css} }} </style>"

if final_css != "":
    st.markdown(final_css, unsafe_allow_html=True)

if conf.get("gemini_api_key"):
    genai.configure(api_key=conf.get("gemini_api_key"))

# --- 個人設定（CSS）の適用 ---
custom_css = "<style>"
if conf.get("font_size") == "大きめ":
    custom_css += "p, li, .katex, .stMarkdown { font-size: 1.2rem !important; } "
elif conf.get("font_size") == "特大":
    custom_css += "p, li, .katex, .stMarkdown { font-size: 1.5rem !important; } "

if conf.get("font_family") == "明朝体 (試験本番風)":
    custom_css += "p, li, h1, h2, h3, .stMarkdown { font-family: 'Noto Serif JP', serif !important; } "
custom_css += "</style>"


st.markdown(custom_css, unsafe_allow_html=True)

data = load_data()

# アプリの初期状態を設定
if "mode" not in st.session_state:
    startup = conf.get("startup_mode", "ホーム")
    if startup == "ランダム演習": st.session_state.mode = "quiz"
    elif startup == "成績リスト": st.session_state.mode = "dashboard"
    else: st.session_state.mode = "home"

if "quiz_mode" not in st.session_state: st.session_state.quiz_mode = "random"
if "current_q" not in st.session_state: st.session_state.current_q = None
if "current_genre" not in st.session_state: st.session_state.current_genre = None
if "show_answer" not in st.session_state: st.session_state.show_answer = False
if "selected_tag" not in st.session_state: st.session_state.selected_tag = None
if "seq_list" not in st.session_state: st.session_state.seq_list = []
if "seq_idx" not in st.session_state: st.session_state.seq_idx = 0
if "just_completed" not in st.session_state: st.session_state.just_completed = False
if "chat_history" not in st.session_state: st.session_state.chat_history = []
if "chat_q_key" not in st.session_state: st.session_state.chat_q_key = None

# ==========================================
# サイドバー
# ==========================================
st.sidebar.title("メニュー")
if st.sidebar.button("🐾 ホーム", use_container_width=True):
    st.session_state.mode = "home"
    st.rerun()

if st.sidebar.button("📝 ランダム演習", use_container_width=True):
    st.session_state.mode = "quiz"
    st.session_state.quiz_mode = "random"
    st.session_state.current_q = None
    st.rerun()

if st.sidebar.button("🎯 頻出＆弱点特訓 (AIおすすめ)", use_container_width=True):
    st.session_state.mode = "recommend_setup"
    st.rerun()

if st.sidebar.button("🛤️ 順番に解く（コース）", use_container_width=True):
    st.session_state.mode = "seq_setup"
    st.rerun()

if st.sidebar.button("📊 成績リスト (年度別)", use_container_width=True):
    st.session_state.mode = "dashboard"
    st.rerun()

if st.sidebar.button("🔍 タグ検索　＆　問題履歴", use_container_width=True):
    st.session_state.mode = "tag_search"
    st.rerun()

st.sidebar.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
if st.sidebar.button("✨ AI問題追加 (自動)", use_container_width=True):
    st.session_state.mode = "ai_add"
    st.rerun()

if st.sidebar.button("🏆 ランキング", use_container_width=True):
    st.session_state.mode = "ranking"
    st.rerun()
    
st.sidebar.markdown("<br>", unsafe_allow_html=True)
if st.sidebar.button("⚙️ 個人設定", use_container_width=True):
    st.session_state.mode = "settings"
    st.rerun()

# ==========================================
# メイン画面
# ==========================================
# --- フッター（すべての画面で表示） ---
st.markdown("<hr style='border: 0.5px solid #444;'/>", unsafe_allow_html=True)
st.markdown("""
<div style="font-size: 0.8em; color: #888; text-align: center;">
    <b>【利用規約・著作権について】</b><br>
    入試問題は受験予定者が受験の準備に使用することや、教育機関の教職員が教育の一環として使用することを目的としています。
</div>
""", unsafe_allow_html=True)

if not data and st.session_state.mode not in ["settings", "ai_add"]:
    st.warning("まだ問題データが登録されていません．「✨ AI問題追加」からデータを登録してください．")
else:
    # --------------------------------------
    # モード：ホーム
    # --------------------------------------
    if st.session_state.mode == "home":
        if st.session_state.just_completed:
            st.success("🎉 コースのすべての問題を解き終えました！お疲れ様でした！")
            if conf.get("show_balloons", True): st.balloons()
            st.session_state.just_completed = False

        st.markdown("<h1 style='text-align: center;'>🏠 学習ホーム</h1>", unsafe_allow_html=True)
        
        current_user = st.session_state.get("username", "Guest")
        st.markdown(f"<p style='text-align: center; font-size: 1.2em;'>ようこそ、<b>{current_user}</b> さん！今日も学習を頑張りましょう🐾</p>", unsafe_allow_html=True)

        target_date_str = conf.get("target_date", "2026-08-20")
        exam_name = conf.get("exam_name", "大阪公立大学大学院 院試")
        
        target_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
        today = datetime.date.today()
        days_left = (target_date - today).days
        
        st.markdown(f"""
        <div style="background-color: rgba(255, 182, 193, 0.15); padding: 20px; border-radius: 15px; text-align: center; margin-bottom: 20px; border: 2px dashed #FF9EAA;">
            <h2 style="margin: 0;">{exam_name} まで</h2>
            <h1 style="margin: 0; font-size: 3em; color: #ff4b4b;">あと {days_left} 日</h1>
        </div>
        """, unsafe_allow_html=True)
        
        # --- カウントダウンとTOEICスコアの設定 ---
        with st.expander("⚙️ 目標設定（カウントダウン・TOEICスコア）", expanded=False):
            new_name = st.text_input("目標名", value=exam_name)
            new_date = st.date_input("目標日", value=target_date)
            
            toeic_val = conf.get("toeic_score", 0)
            new_toeic = st.number_input("TOEICスコア (英語 100点満点への換算用)", min_value=0, max_value=990, value=toeic_val, step=5)
            
            if st.button("設定を保存"):
                conf["exam_name"] = new_name
                conf["target_date"] = new_date.strftime("%Y-%m-%d")
                conf["toeic_score"] = new_toeic
                save_config(conf)
                st.success("保存しました！")
                st.rerun()

        current_user = st.session_state.get("username", "Guest")
        evals_home = load_evals(current_user)
        total_solved = 0
        
        # 評価済みの問題数だけをシンプルにカウント
        for g, qs in evals_home.items():
            for k, val in qs.items():
                r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                if r in ["〇", "△", "▲", "×"]:
                    total_solved += 1
        
        # 画面中央に目立つように配置 ＋ 右側にランキングTOP3
        # 画面中央に目立つように配置 ＋ 右側にランキングTOP3
        col_main, col_rank = st.columns([5, 3])
        
        with col_main:
            # --- 💡 ランク計算を追加 ---
            rank_threshold = 10
            current_rank_idx = total_solved // rank_threshold
            rank_names = ["猫見習い 🐾", "駆け出しハンター 🐈", "一人前キャット 🐅", "ベテラン猫 🦁", "伝説の猫 👑"]
            current_rank = rank_names[min(current_rank_idx, len(rank_names)-1)]
            next_req = rank_threshold - (total_solved % rank_threshold)
            progress = (total_solved % rank_threshold) / rank_threshold
            
            # 💡 注意：この中の """ のすぐ後に ```html などを書かないでください！
            st.markdown(f"""
            <div style='background-color: rgba(255, 255, 255, 0.05); padding: 20px; border-radius: 10px; text-align: center; margin: 0 auto;'>
                <div style='font-size: 1.2em; color: #FFD700; margin-bottom: 5px;'>現在のランク: <b>{current_rank}</b></div>
                <div style='color: #aaa; font-size: 0.9em; margin-bottom: 5px;'>次のランクまであと {next_req} 問！</div>
                <progress value="{progress}" max="1" style="width: 80%; height: 8px; margin-bottom: 15px;"></progress>
                <div style='color: #aaa; font-size: 1.1em;'>🔥 これまでに解き明かした問題数</div>
                <div style='font-size: 3.5em; font-weight: bold; color: #00cc96; line-height: 1.2;'>{total_solved} <span style='font-size: 0.35em; color: #aaa; font-weight: normal;'>問</span></div>
            </div>
            """, unsafe_allow_html=True)
            
        with col_rank:
            ranking_list = get_ranking_data(data)
            top_solved = sorted(ranking_list, key=lambda x: x["solved"], reverse=True)[:3]
            
            st.markdown("<div style='color: #aaa; font-size: 0.9em; margin-bottom: 5px;'>🏆 問題数 トップ3</div>", unsafe_allow_html=True)
            medals = ["🥇", "🥈", "🥉"]
            for i, user_data in enumerate(top_solved):
                st.markdown(f"""
                <div style='background-color: rgba(255,255,255,0.03); padding: 8px 15px; border-left: 3px solid #FFD700; border-radius: 4px; margin-bottom: 5px; display: flex; justify-content: space-between;'>
                    <span>{medals[i]} <b>{user_data['name']}</b></span>
                    <span style='color: #00cc96; font-weight: bold;'>{user_data['solved']} 問</span>
                </div>
                """, unsafe_allow_html=True)

        # --- 📊 スコア計算 (4段階評価・部分点対応) ---

        def calculate_advanced_expected_scores(data, evals):
            """
            試験の特性に合わせた高度な予想得点計算
            - 数学: 問題番号(1〜4)ごとの25点満点加算方式
            - 電磁気・電気回路: 全タグの出現頻度を母数とした「習熟度」加重平均方式
            """
            scores = {"数学": 0.0, "電磁気": 0.0, "電気回路": 0.0}
    
            rating_map = {"〇": 1.0, "△": 0.66, "▲": 0.33, "×": 0.0}

            # ==========================================
            # 📐 数学の計算（問題1〜4の固定配点モデル）
            # ==========================================
            if "数学" in evals and "数学" in data:
                math_total = 0.0
                for q_num in ["1", "2", "3", "4"]:
                    num_evals = []
                    for k, val in evals["数学"].items():
                        if str(k).endswith(q_num):
                            r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                            if r in rating_map:
                                num_evals.append(rating_map[r])
            
                    if num_evals:
                        math_total += 25.0 * (sum(num_evals) / len(num_evals))
                scores["数学"] = round(math_total, 1)

            # ==========================================
            # ⚡ 電磁気・電気回路の計算（真・タグ習熟度モデル ＋ フォールバック）
            # ==========================================
            for genre in ["電磁気", "電気回路"]:
                if genre in evals and genre in data:
                    tag_counts = {}
                    for q in data[genre]:
                        for t in q.get("tags", []):
                            tag_counts[t] = tag_counts.get(t, 0) + 1

                    # 💡 追加：もしその分野にタグが1つも登録されていない場合は、単純な正答率で計算する
                    if not tag_counts:
                        genre_total_score = 0.0
                        genre_ans_count = 0
                        for q_key, val in evals[genre].items():
                            r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                            if r in rating_map:
                                genre_total_score += rating_map[r]
                                genre_ans_count += 1
                
                        # 解いた問題の平均点 × その分野の全問題数に対する割合（少し甘めの仮計算）
                        if genre_ans_count > 0:
                            scores[genre] = round(100.0 * (genre_total_score / len(data[genre])), 1)
                        continue

                    tag_mastery_sum = {t: 0.0 for t in tag_counts}
                    tag_mastery_cnt = {t: 0 for t in tag_counts}
            
                    for q_key, val in evals[genre].items():
                        r = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                        if r not in rating_map: continue
                        ratio = rating_map[r]
                
                        tags = val.get("tags", []) if isinstance(val, dict) else []
                        if not tags:
                            for q in data[genre]:
                                if str(q.get('number', '')) in str(q_key) and str(q.get('year', '')) in str(q_key):
                                    tags = q.get("tags", [])
                                    break
                            
                        for t in tags:
                            # 💡 修正：過去のバグで「タグ1, タグ2」と1つに繋がって保存されたデータを分割して救済！
                            for sub_t in t.replace("，", ",").split(","):
                                sub_t = sub_t.strip()
                                if sub_t in tag_mastery_sum:
                                    tag_mastery_sum[sub_t] += ratio
                                    tag_mastery_cnt[sub_t] += 1
            
                    total_weight = 0
                    weighted_score_sum = 0
            
                    for t, count in tag_counts.items():
                        weight = count 
                        total_weight += weight
                
                        if tag_mastery_cnt[t] > 0:
                            mastery = tag_mastery_sum[t] / tag_mastery_cnt[t]
                        else:
                            mastery = 0.0
                    
                        weighted_score_sum += weight * mastery
                
                    if total_weight > 0:
                        scores[genre] = round(100.0 * (weighted_score_sum / total_weight), 1)

            return scores

        current_user = st.session_state.get("username", "Guest")
        evals = load_evals(current_user)
        total_ans = 0
        total_score = 0.0
        
        genre_stats = {g: {"ans": 0, "score": 0.0} for g in GENRE_ORDER}
        tag_stats = {g: {} for g in GENRE_ORDER}

        for g, qs in evals.items():
            if g not in genre_stats: continue
            for k, val in qs.items():
                rating = val.get("rating", "") if isinstance(val, dict) else (val if isinstance(val, str) else "")
                tags = val.get("tags", []) if isinstance(val, dict) else []

                # 4段階評価（〇, △, ▲, ×）のものだけカウント
                if rating in ["〇", "△", "▲", "×"]:
                    total_ans += 1
                    genre_stats[g]["ans"] += 1
                    
                    # 記述式試験の部分点（〇: 1.0, △: 0.66, ▲: 0.33, ×: 0.0）
                    if rating == "〇": pts = 1.0
                    elif rating == "△": pts = 0.66
                    elif rating == "▲": pts = 0.33
                    else: pts = 0.0
                        
                    total_score += pts
                    genre_stats[g]["score"] += pts

                    for t in tags:
                        if t not in tag_stats[g]:
                            tag_stats[g][t] = {"ans": 0, "score": 0.0}
                        tag_stats[g][t]["ans"] += 1
                        tag_stats[g][t]["score"] += pts

        # --- 🎯 合格ボーダー分析 (円グラフ) ---
        final_genre_scores = calculate_advanced_expected_scores(data, evals)

        if conf.get("toeic_score", 0)>=800:
              english_score = 100.0
        else:
              english_score = (conf.get("toeic_score", 0) / 800.0) * 100.0
        total_400_score = final_genre_scores.get("電気回路", 0) + final_genre_scores.get("電磁気", 0) + final_genre_scores.get("数学", 0) + english_score

        st.markdown("<h3 style='text-align: center; margin-top: 20px;'>🎯 合格ボーダー分析 (400点満点)</h3>", unsafe_allow_html=True)
        
        fig = go.Figure(go.Indicator(
              mode = "gauge+number",
              value = total_400_score,
              number = {'suffix': " 点", 'valueformat': ".1f"},
              domain = {'x': [0, 1], 'y': [0, 1]},
              title = {'text': "<b>現在の推定スコア</b><br><span style='color: gray; font-size:0.8em'>ボーダー: 240点 (得点率6割)</span>"},
              gauge = {
                  'axis': {'range': [None, 400], 'tickwidth': 1, 'tickcolor': "white"},
                  'bar': {'color': "#ff4b4b" if total_400_score < 240 else "#00cc96"},
                  'bgcolor': "rgba(0,0,0,0)",
                  'borderwidth': 2,
                  'bordercolor': "gray",
                  'steps': [
                      {'range': [0, 240], 'color': "rgba(255, 75, 75, 0.2)"},
                      {'range': [240, 400], 'color': "rgba(0, 204, 150, 0.2)"}],
                  'threshold': {
                      'line': {'color': "red", 'width': 4},
                      'thickness': 0.75,
                      'value': 240}
              }
          ))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", height=350, margin=dict(t=50, b=20))
        st.plotly_chart(fig, use_container_width=True)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("⚡ 電気回路", f"{final_genre_scores.get('電気回路', 0):.1f} / 100")
        col2.metric("🧲 電磁気", f"{final_genre_scores.get('電磁気', 0):.1f} / 100")
        col3.metric("📐 数学", f"{final_genre_scores.get('数学', 0):.1f} / 100")
        col4.metric("🔤 英語 (TOEIC)", f"{english_score:.1f} / 100")

          # 💡 追加点：計算方法の解説をグラフの下に小さく表示
        st.markdown("""
          <div style='background-color: rgba(255, 255, 255, 0.05); padding: 10px; border-radius: 5px; font-size: 0.8em; color: #aaa; margin-bottom: 20px;'>
              <b>📝 予想スコアの算出ロジック</b><br>
              ・<b>数学</b>：問題番号(1〜4)ごとに独立して平均正答率を算出し，それぞれ25点満点で加算しています．<br>
              ・<b>専門科目(電気回路・電磁気)</b>：過去問全体での「タグ(出題分野)の出現頻度」を重みとして計算しています．毎年出題される重要分野を間違えるとスコアが大きく下がり，稀な問題を間違えても影響が少ない実践的なモデルです．
          </div>
        """, unsafe_allow_html=True)

        # --- 📊 過去問道場風 詳細レポート (部分点対応) ---
        st.markdown("<h3 style='text-align: center; margin-top: 30px;'>📊 詳細成績レポート</h3>", unsafe_allow_html=True)

        st.markdown("#### 🎯 全体")
        c1, c2, c3 = st.columns(3)
        overall_acc = (total_score / total_ans * 100) if total_ans > 0 else 0.0
        
        with c1:
            st.markdown(f"<div style='text-align: center; color: #888;'>解答済みの問題数</div><h2 style='text-align: center;'>{total_ans} <span style='font-size: 0.5em;'>問</span></h2>", unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div style='text-align: center; color: #888;'>予想スコア (400点満点)</div><h2 style='text-align: center;'>{total_400_score:.1f} <span style='font-size: 0.5em;'>点</span></h2>", unsafe_allow_html=True)
        with c3:
            st.markdown(f"<div style='text-align: center; color: #888;'>総合得点率</div><h2 style='text-align: center;'>{overall_acc:.1f} <span style='font-size: 0.5em;'>%</span></h2>", unsafe_allow_html=True)
        
        st.progress(int(overall_acc))
        st.markdown("<hr style='margin: 1.5em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)

        col_g, col_t = st.columns([1, 1])
        with col_g:
            st.markdown("#### 📁 分野別")
            for g in GENRE_ORDER:
                ans = genre_stats[g]["ans"]
                score = genre_stats[g]["score"]
                acc = (score / ans * 100) if ans > 0 else 0.0
                
                st.markdown(f"""
                <div style="display: flex; justify-content: space-between; margin-bottom: -10px;">
                    <div><b>{g}</b> <span style="color:#888; font-size:0.8em;">解答 {ans}問</span></div>
                    <div><b>{acc:.1f}%</b></div>
                </div>
                """, unsafe_allow_html=True)
                st.progress(int(acc))
                st.write("")
            # ==========================================
            # 💡 ここから追加：最近解いた問題の履歴 (3件)
            # ==========================================
            st.markdown("<hr style='margin: 1.5em 0px; border: 0.5px dashed #444;'/>", unsafe_allow_html=True)
            st.markdown("#### 🕒 最近の学習履歴")
     
            # 全ての評価データからリスト化
            recent_history = []
            for g_name, qs in evals.items():
                for k, val in qs.items():
                    if isinstance(val, dict):
                        r = val.get("rating", "")
                        ts = val.get("timestamp", 0.0) # 保存した時間を取得（古いデータは0になる）
                    else:
                        r = val if isinstance(val, str) else ""
                        ts = 0.0
                        
                    if r in ["〇", "△", "▲", "×"]:
                       recent_history.append({"genre": g_name, "q_key": k, "rating": r, "timestamp": ts})
    
            # 💡 修正：辞書の順番ではなく、保存した「時間」の順番で正確に並び替える
            recent_history.sort(key=lambda x: x["timestamp"])
            
            # 最新の3件を取得して反転
            recent_history = recent_history[-3:]
            recent_history.reverse() 
            
            if recent_history:
                rating_icons = {"〇": "🟢", "△": "🟡", "▲": "🟠", "×": "🔴"}
                for i, item in enumerate(recent_history):
                    parts = item['q_key'].split('_')
                    # 💡 修正：年度がダブらないように元の文字から「年度」を削り取っておく
                    y_str = parts[0].replace("年度", "") if len(parts) > 0 else ""
                    num_str = parts[1].replace("問題", "").strip() if len(parts) > 1 else ""
                    icon = rating_icons.get(item['rating'], "⚪")
                    
                    q_match = next((q for q in data.get(item['genre'], []) if f"{q.get('year', '')}_{q.get('number', '')}" == item['q_key']), None)
                    
                    if q_match:
                        col_hist1, col_hist2 = st.columns([5, 2])
                        with col_hist1:
                            st.markdown(f"""
                            <div style="background-color: rgba(255,255,255,0.03); padding: 8px 15px; border-left: 4px solid #444; border-radius: 4px; margin-bottom: 5px;">
                                <span style="font-size: 0.75em; color: #aaa;">{y_str}年度 ｜ {item['genre']}</span><br>
                                <span style="font-size: 1.2em; margin-right: 5px;">{icon}</span><span style="font-weight: bold; font-size: 0.95em;">問 {num_str}</span>
                            </div>
                            """, unsafe_allow_html=True)
                            
                        with col_hist2:
                            st.write("") 
                            if st.button("復習する", key=f"hist_jump_{i}_{item['q_key']}", use_container_width=True):
                                st.session_state.mode = "quiz"
                                st.session_state.quiz_mode = "random" 
                                st.session_state.current_genre = item['genre']
                                st.session_state.current_q = q_match
                                st.session_state.show_answer = False
                                st.rerun()
            else:
                st.markdown("<div style='color: #666; font-size: 0.9em; text-align: center; padding: 10px;'>まだ履歴がありません</div>", unsafe_allow_html=True)
                

            st.write("")
            if st.button("🕒 もっと履歴を見る（直近20件）", use_container_width=True):
                st.session_state.mode = "tag_search"
                st.rerun()


        with col_t:
          st.markdown("#### 🚨 苦手分野 ワースト5 (要復習)")
      
          # 1. 全ジャンルのタグデータを一つのリストにまとめる
          all_tags_list = []
          for g in GENRE_ORDER:
              for t, stats in tag_stats[g].items():
                  ans = stats["ans"]
                  if ans > 0:  # 1問以上解いたタグのみ対象
                      score = stats["score"]
                      acc = (score / ans * 100)
                      all_tags_list.append({
                          "tag": t,
                          "genre": g,
                          "ans": ans,
                          "acc": acc
                      })
      
          # 2. 正答率が「低い順（昇順）」に並び替える
          all_tags_list.sort(key=lambda x: x["acc"])
      
          # 3. トップ5（ワースト5）を抽出
          worst_5 = all_tags_list[:5]
      
          if worst_5:
              rank_icons = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
              for i, item in enumerate(worst_5):
                  st.markdown(f"""
                  <div style="display: flex; justify-content: space-between; margin-bottom: -10px;">
                      <div>{rank_icons[i]} <b>{item['tag']}</b> <span style="color:#888; font-size:0.8em;">({item['genre']}) 解答 {item['ans']}問</span></div>
                      <div style="color: #ff4b4b;"><b>{item['acc']:.1f}%</b></div>
                  </div>
                  """, unsafe_allow_html=True)
                  st.progress(int(item['acc']))
                  st.write("")
          else:
              st.info("まだ評価済みのデータがありません．")

    # --------------------------------------
    # モード：AI問題追加
    # --------------------------------------
    elif st.session_state.mode == "ai_add":
        st.title("✨ AIで問題を全自動追加")
        st.write("過去問の画像をアップロードするだけで，AIが解答を作成し，アプリに自動登録します！")
        
        if not conf.get("gemini_api_key"):
            st.error("⚠️ AI機能を使用するには、「⚙️ 個人設定」から Gemini APIキー を設定してください。")
            st.stop()
            
        col1, col2, col3 = st.columns(3)
        with col1: add_year = st.text_input("年度 (例: 2024.8年度)", value="2024.8年度")
        with col2: add_genre = st.selectbox("分野", GENRE_ORDER + ["その他"])
        with col3: add_number = st.text_input("問題番号 (例: 問題1)", value="問題1")
        
        uploaded_file = st.file_uploader("問題の画像を選択してください (.png, .jpg)", type=["png", "jpg", "jpeg"])
        
        if uploaded_file is not None:
            st.image(uploaded_file, width=400)
            
            if st.button("🤖 AIで解析して自動登録する", type="primary"):
                with st.spinner("AIが解答を作成中... (約15〜30秒かかります)"):
                    res_text = "（AIからの応答を取得する前にエラーが発生しました）" 
                    try:
                        os.makedirs("images", exist_ok=True)
                        file_ext = uploaded_file.name.split('.')[-1]
                        safe_year = add_year.replace("年度", "")
                        safe_num = add_number.replace("問題", "")
                        img_filename = f"{safe_year}_{add_genre}_{safe_num}.{file_ext}"
                        img_path = os.path.join("images", img_filename).replace("\\", "/")
                        
                        with open(img_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                            
                        img = Image.open(uploaded_file)
                        
                        # エラー対策：最新モデルを指定し、失敗時は予備モデルを使用
                        model = genai.GenerativeModel('gemini-3.1-flash-lite')
                        
                        prompt = f"""
                        以下の問題画像の解答・解説を作成し，指定されたJSONフォーマットのみを出力してください．余計な文章は一切不要です．
                        
                        【出力ルール・JSONキーの指定】
                        ```json
                        {{
                          "{add_genre}": [
                            {{
                              "year": "{add_year}",
                              "number": "{add_number}",
                              "question_image": "{img_path}",
                              "answer": "解答のテキスト",
                              "tags": ["タグ1", "タグ2"]
                            }}
                          ]
                        }}
                        ```
                        【LaTeX・テキストの厳格な記述ルール】
                        - 計算の途中式を、あきらかな場合を除き明示すること．
                        - 文章は日本語とし，句読点は「．」「，」を使用すること．
                        - 解法に関するキーワードを3〜5個抽出し，`tags` に含めること．
                        - 数式は必ず LaTeX 形式とし，文章中の数式は `$`，独立した数式ブロックは `$$` で正確に囲むこと．
                        - $ や $$ の中に、日本語の文字を含めないこと．
                        - JSONの仕様上、バックスラッシュは必ず2重にすること（例：\\\\frac）．行列の改行は \\\\\\\\ とすること．
                        - 改行は \\n を記述すること．
                        """
                        
                        response = model.generate_content([prompt, img])
                        res_text = response.text
                        
                        if "```json" in res_text:
                            res_text = res_text.split("```json")[1].split("```")[0].strip()
                        elif "```" in res_text:
                            res_text = res_text.split("```")[1].split("```")[0].strip()
                            
                        new_data = json.loads(res_text)
                        
                        for g, qs in new_data.items():
                            if g not in data:
                                data[g] = []
                            data[g].extend(qs)
                        
                        save_data(data)
                        st.success(f"🎉 自動登録が完了しました！ ({add_year} {add_genre} {add_number})")
                        
                    except Exception as e:
                        st.error(f"AIとの通信中にエラーが発生しました: {e}")
                        st.write("▼ 出力情報（エラーの手掛かり）:")
                        st.write(res_text)

    # --------------------------------------
    # モード：個人設定
    # --------------------------------------
    elif st.session_state.mode == "settings":
        st.markdown("<h1 style='text-align: center;'>⚙️ 個人設定</h1>", unsafe_allow_html=True)
        
        current_user = st.session_state.get("username", "Guest")
        st.markdown(f"<p style='text-align: center;'>現在のアカウント: <b>{current_user}</b></p>", unsafe_allow_html=True)

        st.markdown("### 🤖 AI連携設定")
        gemini_key = st.text_input("Gemini APIキー", value=conf.get("gemini_api_key", ""), type="password")
        st.caption("AIチャットや自動問題追加機能を使用するために必要です。[Google AI Studio](https://aistudio.google.com/) から無料で取得できます。")

        st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
        st.markdown("### 👁️ 見た目の設定")
        # テーマ選択の追加
        theme_list = ["標準 (デフォルト)", "猫テーマ (システム設定に従う)", "お昼の猫カフェ (ライト)", "夜の猫カフェ (ダーク)"]
        curr_theme = conf.get("theme_mode", "標準 (デフォルト)")
        selected_theme = st.radio("🎨 アプリのテーマ", theme_list, index=theme_list.index(curr_theme) if curr_theme in theme_list else 0)
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            font_size_list = ["標準", "大きめ", "特大"]
            curr_fsize = conf.get("font_size", "標準")
            font_size = st.radio("文字サイズ（問題文・解答・数式）", font_size_list, index=font_size_list.index(curr_fsize) if curr_fsize in font_size_list else 0)
        with col_s2:
            font_fam_list = ["標準 (ゴシック体)", "明朝体 (試験本番風)"]
            curr_ffam = conf.get("font_family", "標準 (ゴシック体)")
            font_family = st.radio("文字の書体", font_fam_list, index=font_fam_list.index(curr_ffam) if curr_ffam in font_fam_list else 0)
            
        img_width = st.slider("問題画像の表示サイズ (px)", min_value=300, max_value=1200, value=conf.get("image_width", 700), step=50)
        
        st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
        st.markdown("### 🛠️ 動作の設定")
        
        startup_list = ["ホーム", "ランダム演習", "成績リスト"]
        curr_startup = conf.get("startup_mode", "ホーム")
        startup_mode = st.selectbox("アプリ起動時に最初に表示する画面", startup_list, index=startup_list.index(curr_startup) if curr_startup in startup_list else 0)
        show_balloons = st.checkbox("コース完了時に達成のお祝い（風船アニメーション）を表示する", value=conf.get("show_balloons", True))
        
        st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
        st.markdown("### 🏷️ タグの整理・統合")
        st.write("似たようなタグを一つにまとめたり，名前を変更したりできます．")
        
        # --- 🔒 ここから管理者（用皆樹さん）専用のロック ---
        if st.session_state.get("username") == "用皆樹":
            st.markdown("## 🏷️ タグの管理・クラウド移行（管理者専用）")
            
            # 全タグの取得
            all_tags_for_edit = set()
            evals_for_edit = load_evals(current_user)
            for g, qs in data.items():
                for q in qs:
                    q_k = f"{q.get('year', '')}_{q.get('number', '')}"
                    r_data = evals_for_edit.get(g, {}).get(q_k)
                    if isinstance(r_data, dict):
                        all_tags_for_edit.update(r_data.get("tags", []))
                    else:
                        all_tags_for_edit.update(q.get("tags", []))

            if all_tags_for_edit:
                st.write("▼ 現在の全タグ（右上のコピーボタンを押して、私に送ってください！）")
                st.code(", ".join(sorted(list(all_tags_for_edit))))
            
            if all_tags_for_edit:
                all_tags_list = sorted(list(all_tags_for_edit))
                
                old_tags = st.multiselect("まとめたい古いタグを選んでください（複数選択可）", all_tags_list)
                new_tag = st.text_input("新しいタグ名（統合先）", placeholder="例：微分方程式")
                
                if st.button("選択したタグをまとめて書き換える", type="primary"):
                    if old_tags and new_tag.strip() != "":
                        # questions.json の書き換え
                        for g in data:
                            for q in data[g]:
                                if any(t in q.get("tags", []) for t in old_tags):
                                    q["tags"] = [new_tag if t in old_tags else t for t in q["tags"]]
                                    q["tags"] = list(dict.fromkeys(q["tags"]))
                        save_data(data)
                        
                        # evaluations.json の書き換え
                        for g in evals_for_edit:
                            for k in evals_for_edit[g]:
                                if isinstance(evals_for_edit[g][k], dict) and any(t in evals_for_edit[g][k].get("tags", []) for t in old_tags):
                                    evals_for_edit[g][k]["tags"] = [new_tag if t in old_tags else t for t in evals_for_edit[g][k]["tags"]]
                                    evals_for_edit[g][k]["tags"] = list(dict.fromkeys(evals_for_edit[g][k]["tags"]))
                        save_evals(evals_for_edit)
                        
                        st.success(f"選択したタグを「{new_tag}」にまとめて統合しました！")
                        st.rerun()
                    elif not old_tags:
                        st.warning("まとめたい古いタグを選択してください．")
                    elif new_tag.strip() == "":
                        st.warning("新しいタグ名を入力してください．")

            # クラウド移行用の一時ボタン
            st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
            st.markdown("### ☁️ クラウドへのデータ移行")
            st.write("手元の questions.json をデータベースにアップロードします．")
            
            if st.button("データをクラウドに移行する", type="primary"):
                import os
                import json
                if os.path.exists("questions.json"):
                    with open("questions.json", "r", encoding="utf-8") as f:
                        q_data = json.load(f)
                        db.reference('app_data/all_questions').set(q_data)
                    st.success("🎉 問題データの移行が完了しました！アプリを更新すると問題が表示されます！")
                else:
                    st.error("⚠️ questions.json が見つかりませんでした．")
                    
        else:
            # 用皆樹さん以外のユーザーがアクセスした時の表示
            st.warning("🔒 「タグの整理」および「クラウド移行」は管理者専用機能のためロックされています．")
        # --- 🔒 ここまで管理者専用のロック ---

        # ⚙️ 設定保存ボタンは全員が使えるように if-else の外側に残します
        st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
        st.write("")
        if st.button("設定を保存する", type="primary", use_container_width=True):
            conf["gemini_api_key"] = gemini_key
            conf["font_size"] = font_size
            conf["font_family"] = font_family
            conf["image_width"] = img_width
            conf["startup_mode"] = startup_mode
            conf["theme_mode"] = selected_theme
            conf["show_balloons"] = show_balloons
            save_config(conf)
            st.success("設定を保存しました！")
            st.rerun()



    # --------------------------------------
    # モード：ランキング
    # --------------------------------------
    elif st.session_state.mode == "ranking":
        st.markdown("<h1 style='text-align: center;'>🏆 ユーザーランキング</h1>", unsafe_allow_html=True)
        st.write("全員の学習状況と予想スコアのランキングです．ライバルたちと競い合いましょう！")
        
        st.info("💡 ランキングのデータは **2時間に1回** のペースで自動更新されます．")

        with st.spinner("ランキングを集計中..."):
            ranking_list = get_ranking_data(data)
            
        if not ranking_list:
            st.info("まだデータがありません．")
        else:
            col_rank1, col_space, col_rank2 = st.columns([10, 1, 10])
            
            with col_rank1:
                st.markdown("### 🔥 解答問題数 ランキング")
                solved_ranking = sorted(ranking_list, key=lambda x: x["solved"], reverse=True)
                for i, u in enumerate(solved_ranking):
                    rank_icon = ["🥇", "🥈", "🥉"][i] if i < 3 else f"<b>{i+1}位</b>"
                    is_me = "border: 2px solid #FF69B4;" if u["name"] == st.session_state.username else "border: 1px solid #444;"
                    st.markdown(f"""
                    <div style='background-color: rgba(255,255,255,0.03); padding: 10px 15px; border-radius: 8px; margin-bottom: 8px; {is_me} display: flex; justify-content: space-between; align-items: center;'>
                        <div style='font-size: 1.1em;'>{rank_icon} <span style='margin-left: 10px;'>{u['name']}</span></div>
                        <div style='font-size: 1.2em; font-weight: bold; color: #00cc96;'>{u['solved']} <span style='font-size: 0.6em; color: #aaa;'>問</span></div>
                    </div>
                    """, unsafe_allow_html=True)

            with col_rank2:
                st.markdown("### 🎯 予想スコア ランキング (400点満点)")
                score_ranking = sorted(ranking_list, key=lambda x: x["score"], reverse=True)
                for i, u in enumerate(score_ranking):
                    rank_icon = ["🥇", "🥈", "🥉"][i] if i < 3 else f"<b>{i+1}位</b>"
                    is_me = "border: 2px solid #FF69B4;" if u["name"] == st.session_state.username else "border: 1px solid #444;"
                    st.markdown(f"""
                    <div style='background-color: rgba(255,255,255,0.03); padding: 10px 15px; border-radius: 8px; margin-bottom: 8px; {is_me} display: flex; justify-content: space-between; align-items: center;'>
                        <div style='font-size: 1.1em;'>{rank_icon} <span style='margin-left: 10px;'>{u['name']}</span></div>
                        <div style='font-size: 1.2em; font-weight: bold; color: #ff4b4b;'>{u['score']:.1f} <span style='font-size: 0.6em; color: #aaa;'>点</span></div>
                    </div>
                    """, unsafe_allow_html=True)




    # --------------------------------------
    # モード：AIおすすめ特訓（頻出＆弱点）
    # --------------------------------------
    elif st.session_state.mode == "recommend_setup":
        st.markdown("<h2 style='text-align: center;'>🎯 AIおすすめ特訓 (頻出 ＆ 弱点)</h2>", unsafe_allow_html=True)
        st.markdown("<p style='text-align: center;'>出題頻度が高く、かつあなたが苦手としている（または未着手の）問題を優先的にピックアップします．</p>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            target_genre = st.selectbox("📁 対策する分野", ["全分野からミックス"] + GENRE_ORDER)
        with col2:
            num_recommend = st.number_input("🎯 出題数", min_value=1, max_value=20, value=5)
            
        st.write("")
        
        if st.button("🚀 おすすめコースを生成してスタート！", type="primary", use_container_width=True):
            current_user = st.session_state.get("username", "Guest")
            evals = load_evals(current_user)
            
            # 💡 改善1：指定された分野のタグだけを集計する（ムダな計算を省いて高速化）
            tag_counts = {}
            for g, qs in data.items():
                if target_genre != "全分野からミックス" and g != target_genre:
                    continue # 対象外の分野はタグ集計からも除外する
                    
                for q in qs:
                    for t in q.get("tags", []):
                        tag_counts[t] = tag_counts.get(t, 0) + 1
            
            # 2. 全問題に「おすすめ度スコア」をつける
            scored_qs = []
            import random # 同点シャッフル用
            
            for g, qs in data.items():
                if target_genre != "全分野からミックス" and g != target_genre:
                    continue
                    
                # 💡 改善2：分野ごとの成績を先に変数に入れておく（ループのたびに辞書を探す処理を減らして高速化）
                g_evals = evals.get(g, {})
                    
                for q in qs:
                    q_key = f"{q.get('year', '')}_{q.get('number', '')}"
                    rating_data = g_evals.get(q_key)
                    
                    if rating_data is None: r = ""
                    elif isinstance(rating_data, str): r = rating_data
                    else: r = rating_data.get("rating", "")
                    
                    # 基礎スコア：その問題に含まれるタグの出現回数の合計
                    base_score = sum(tag_counts.get(t, 0) for t in q.get("tags", []))
                    if base_score == 0: base_score = 1 
                    
                    # 💡 改善1：未着手の倍率を「1.0」から「2.0」へ大幅アップ！（一番優先されるようになります）
                    if r == "": mult = 2.0       # 未着手を最優先！
                    elif r == "×": mult = 1.5      
                    elif r == "▲": mult = 1.2
                    elif r == "△": mult = 0.5
                    elif r == "〇": mult = 0.05   
                    else: mult = 1.0
                    
                    # 💡 改善2：ランダム性を「足し算」から「掛け算（±20%の揺らぎ）」に変更！
                    # 例：スコア10の問題が、毎回 8.0 〜 12.0 の間でランダムに変動します
                    final_score = (base_score * mult) * random.uniform(0.8, 1.2)
                    
                    scored_qs.append({"genre": g, "q": q, "score": final_score})
            
            # 3. スコアが高い順に並び替え、上位 N 問を抽出
            scored_qs.sort(key=lambda x: x["score"], reverse=True)
            top_qs = scored_qs[:num_recommend]
            
            if top_qs:
                st.session_state.seq_list = [{"genre": item["genre"], "q": item["q"]} for item in top_qs]
                st.session_state.seq_idx = 0
                st.session_state.quiz_mode = "sequential"
                
                nxt = st.session_state.seq_list[0]
                st.session_state.current_genre = nxt["genre"]
                st.session_state.current_q = nxt["q"]
                st.session_state.mode = "quiz"
                st.session_state.show_answer = False
                st.rerun()
            else:
                st.error("おすすめできる問題が見つかりませんでした．")


    # --------------------------------------
    # モード：順番に解く（コース設定）
    # --------------------------------------
    elif st.session_state.mode == "seq_setup":
        st.markdown("<h2 style='text-align: center;'>🚂 カスタム・シーケンシャルコース</h2>", unsafe_allow_html=True)
        st.markdown("<p style='text-align: center;'>開始問題や出題数を指定して、特定範囲を集中特訓します．</p>", unsafe_allow_html=True)

        col_s1, col_s2 = st.columns(2)
        with col_s1:
            # 1. 分野の選択
            target_genre = st.selectbox("📁 集中する分野", GENRE_ORDER)

            genre_data = load_genre_data(target_genre)
            
            # 選択された分野の問題リストを取得し、年度・問題番号順にソート
            q_list = sorted(genre_data, key=lambda x: (x.get('year', ''), x.get('number', '')))
            q_options = [f"{q.get('year', '')} {q.get('number', '')}" for q in q_list]
            
            # 2. 開始問題の選択
            if q_options:
                start_q_str = st.selectbox("📍 開始問題（ここからスタート）", q_options)
            else:
                start_q_str = None
                st.warning("この分野にはまだ問題が登録されていません．")

        with col_s2:
            # 3. 昇順・降順の選択
            order = st.radio("⏱️ 進む方向", ["昇順 (過去から最新へ進む)", "降順 (最新から過去へ遡る)"])
            
            # 4. 出題数の指定
            max_q_count = len(q_list)
            if max_q_count > 0:
                num_questions = st.number_input("🎯 出題数", min_value=1, max_value=max_q_count, value=min(5, max_q_count))
            else:
                num_questions = 0

        st.write("")
        if st.button("🚂 この設定でスタート！", type="primary", use_container_width=True, disabled=(max_q_count == 0)):
            # 昇順か降順かでリストの並びを決定
            is_reverse = (order == "降順 (最新から過去へ遡る)")
            sorted_q_list = sorted(data[target_genre], key=lambda x: (x.get('year', ''), x.get('number', '')), reverse=is_reverse)
            
            # 開始問題のインデックス（何番目か）を探す
            start_idx = 0
            for i, q in enumerate(sorted_q_list):
                if f"{q.get('year', '')} {q.get('number', '')}" == start_q_str:
                    start_idx = i
                    break
                    
            # 開始位置から指定された問題数だけリストを切り取る
            selected_qs = sorted_q_list[start_idx : start_idx + num_questions]
            
            # 出題用リスト（seq_list）を作成
            st.session_state.seq_list = [{"genre": target_genre, "q": q} for q in selected_qs]
            st.session_state.seq_idx = 0
            st.session_state.quiz_mode = "sequential"
            
            # 最初の問題をセットしてクイズ画面へ
            first_q = st.session_state.seq_list[0]
            st.session_state.current_genre = first_q["genre"]
            st.session_state.current_q = first_q["q"]
            st.session_state.show_answer = False
            st.session_state.ai_generated_answer = None
            st.session_state.mode = "quiz"
            st.rerun()
        st.markdown("<hr style='margin: 1em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            seq_type = st.radio("順番の進め方", ["年度ごとに解く（例：2024年度の電気回路 → 電磁気 → 数学）", "分野ごとに解く（例：電気回路の全年度 → 電磁気の全年度）"])
        with col2:
            order_type = st.radio("年度の順番", ["古い順（過去から順番に）", "新しい順（直近から順番に）"])
            
        st.write("")
        if st.button("🐈💨 このコースでスタート！", type="primary", use_container_width=True):
            seq = [{"genre": g, "q": q} for g, qs in data.items() for q in qs]
            direction = -1 if order_type == "新しい順（直近から順番に）" else 1
            if "年度ごとに" in seq_type:
                seq.sort(key=lambda x: (direction * extract_year_val(x["q"]["year"]), get_genre_idx(x["genre"]), extract_num(x["q"]["number"])))
            else:
                seq.sort(key=lambda x: (get_genre_idx(x["genre"]), direction * extract_year_val(x["q"]["year"]), extract_num(x["q"]["number"])))
                
            st.session_state.seq_list = seq
            st.session_state.seq_idx = 0
            st.session_state.quiz_mode = "sequential"
            
            if seq:
                nxt = seq[0]
                st.session_state.current_genre = nxt["genre"]
                st.session_state.current_q = nxt["q"]
                st.session_state.mode = "quiz"
                st.session_state.show_answer = False
                st.rerun()
            else:
                st.error("問題が存在しません．")

    # --------------------------------------
    # モード：タグ検索
    # --------------------------------------
    elif st.session_state.mode == "tag_search":
        st.markdown("<h1 style='text-align: center;'>🔍 タグ検索 ＆ 分析</h1>", unsafe_allow_html=True)
        current_user = st.session_state.get("username", "Guest")
        evals = load_evals(current_user)

        tab_search, tab_history = st.tabs(["🏷️ タグ検索・分析", "🕒 学習履歴 (直近20件)"])
        
        # ＝＝＝ 🏷️ タブ1：タグ検索 ＝＝＝
        with tab_search:
        
            # --- 追加機能：タグの統計情報を集計 ---
            tag_stats = {}
            for genre, questions in data.items():
                for q in questions:
                    q_key = f"{q.get('year', '')}_{q.get('number', '')}"
                    rating_data = evals.get(genre, {}).get(q_key)
                
                    if rating_data is None: rating, tags = "", q.get("tags", [])
                    elif isinstance(rating_data, str): rating, tags = rating_data, q.get("tags", [])
                    else: rating, tags = rating_data.get("rating", ""), rating_data.get("tags", [])
                
                    for t in tags:
                        if t not in tag_stats:
                            tag_stats[t] = {"count": 0, "〇": 0, "▲": 0, "×": 0, "未": 0}
                        tag_stats[t]["count"] += 1
                        if rating == "〇": tag_stats[t]["〇"] += 1
                        elif rating == "▲": tag_stats[t]["▲"] += 1
                        elif rating == "×": tag_stats[t]["×"] += 1
                        else: tag_stats[t]["未"] += 1
                
            if not tag_stats:
                st.info("まだタグが登録されていません．")
            else:
                all_tags_list = sorted(list(tag_stats.keys()))
            
                # --- 新規追加：タグ一覧と分析データ表 ---
                with st.expander("📊 タグごとの問題数・正答率データを見る", expanded=True):
                    st.write("💡 **表の上の見出し（「問題数」や「正答率」など）をクリックすると，並び替えができます！**")
                
                    table_data = []
                    for t, stats in tag_stats.items():
                        total_eval = stats["〇"] + stats["▲"] + stats["×"]
                        acc = (stats["〇"] / total_eval * 100) if total_eval > 0 else 0.0
                        table_data.append({
                            "タグ名": t,
                            "問題数": stats["count"],
                            "正答率 (%)": round(acc, 1),
                            "🟢 完璧": stats["〇"],
                            "🟡 復習": stats["▲"],
                            "🔴 苦手": stats["×"],
                            "⚪ 未評価": stats["未"]
                        })
                
                    # データフレーム（表）として表示
                    st.dataframe(table_data, use_container_width=True, hide_index=True)

                st.markdown("<hr style='margin: 0.5em 0px; border: 1px solid #555;'/>", unsafe_allow_html=True)
            
                # --- 既存の検索機能 ---
                default_idx = all_tags_list.index(st.session_state.selected_tag) if st.session_state.selected_tag in all_tags_list else 0
                
                selected_tag = st.selectbox("検索するタグを選んでください", all_tags_list, index=default_idx)
                st.session_state.selected_tag = selected_tag
            
                st.markdown(f"<h3 style='text-align: center;'>🏷️ 「{selected_tag}」の検索結果</h3>", unsafe_allow_html=True)
                st.markdown("<hr style='margin: 0.5em 0px; border: 1px solid #555;'/>", unsafe_allow_html=True)
            
                found = False
                for genre in sorted(list(data.keys()), key=get_genre_idx):
                    questions = data[genre]
                    sorted_qs = sorted(questions, key=lambda x: (-extract_year_val(x.get('year', '')), extract_num(x.get('number', ''))))
                
                    for q in sorted_qs:
                        q_key = f"{q.get('year', '')}_{q.get('number', '')}"
                        rating_data = evals.get(genre, {}).get(q_key)
                    
                        if rating_data is None: rating, tags = "", q.get("tags", [])
                        elif isinstance(rating_data, str): rating, tags = rating_data, q.get("tags", [])
                        else: rating, tags = rating_data.get("rating", ""), rating_data.get("tags", [])
                        
                        if selected_tag in tags:
                            found = True
                            rating_icons = {"〇": "🟢 完璧", "▲": "🟡 復習", "×": "🔴 苦手"}
                            status_text = rating_icons.get(rating, "⚪ 未評価")
                        
                            col1, col2, col3 = st.columns([2, 6, 2])
                            with col1: st.write(f"**{status_text}**")
                            with col2:
                                st.write(f"**{q.get('year', '')} {genre} - {q.get('number', '')}**")
                                if tags:
                                    # デザインされたタグを表示
                                    html = '<div style="display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px;">'
                                    for t in tags:
                                        html += f'<span style="background: linear-gradient(135deg, #8a2be2, #4b0082); color: white; padding: 5px 15px; border-radius: 20px; font-size: 14px; border: 1px solid #bda0cb; box-shadow: 0 2px 4px rgba(0,0,0,0.3);">{t}</span>'
                                    html += '</div>'
                                    st.markdown(html, unsafe_allow_html=True)
                            with col3:
                                if st.button("この問題を解く", key=f"tag_search_{genre}_{q_key}", use_container_width=True):
                                    st.session_state.mode = "quiz"
                                    st.session_state.quiz_mode = "random" 
                                    st.session_state.current_genre = genre
                                    st.session_state.current_q = q
                                    st.session_state.show_answer = False
                                    st.rerun()
                            st.markdown("<hr style='margin: 0.2em 0px; border: 0.5px solid #444;'/>", unsafe_allow_html=True)
                if not found:
                    st.write("該当する問題は見つかりませんでした．")


        # ＝＝＝ 🕒 タブ2：直近20件の履歴 ＝＝＝
        with tab_history:
            st.markdown("### 🕒 直近20件の学習履歴")
            
            # 全ての評価データからリスト化（ホーム画面と同じ処理）
            recent_history_20 = []
            for g_name, qs in evals.items():
                for k, val in qs.items():
                    if isinstance(val, dict):
                        r = val.get("rating", "")
                        ts = val.get("timestamp", 0.0)
                    else:
                        r = val if isinstance(val, str) else ""
                        ts = 0.0
                        
                    if r in ["〇", "△", "▲", "×"]:
                       recent_history_20.append({"genre": g_name, "q_key": k, "rating": r, "timestamp": ts})

            # タイムスタンプ順に並び替え
            recent_history_20.sort(key=lambda x: x["timestamp"])
            
            # 💡 最新の20件を取得して反転
            recent_history_20 = recent_history_20[-20:]
            recent_history_20.reverse() 
            
            if recent_history_20:
                rating_icons = {"〇": "🟢", "△": "🟡", "▲": "🟠", "×": "🔴"}
                for i, item in enumerate(recent_history_20):
                    parts = item['q_key'].split('_')
                    y_str = parts[0].replace("年度", "") if len(parts) > 0 else ""
                    num_str = parts[1].replace("問題", "").strip() if len(parts) > 1 else ""
                    icon = rating_icons.get(item['rating'], "⚪")
                    
                    q_match = next((q for q in data.get(item['genre'], []) if f"{q.get('year', '')}_{q.get('number', '')}" == item['q_key']), None)
                    
                    if q_match:
                        # 画面が広く使えるので、ボタンとの比率を [8, 2] にしてゆったり配置
                        col_h1, col_h2 = st.columns([8, 2])
                        with col_h1:
                            st.markdown(f"""
                            <div style="background-color: rgba(255,255,255,0.03); padding: 12px 20px; border-left: 4px solid #444; border-radius: 4px; margin-bottom: 5px;">
                                <span style="font-size: 0.85em; color: #aaa;">{y_str}年度 ｜ {item['genre']}</span><br>
                                <span style="font-size: 1.3em; margin-right: 10px;">{icon}</span><span style="font-weight: bold; font-size: 1.1em;">問 {num_str}</span>
                            </div>
                            """, unsafe_allow_html=True)
                            
                        with col_h2:
                            st.write("")
                            if st.button("復習する", key=f"hist20_jump_{i}_{item['q_key']}", use_container_width=True):
                                st.session_state.mode = "quiz"
                                st.session_state.quiz_mode = "random" 
                                st.session_state.current_genre = item['genre']
                                st.session_state.current_q = q_match
                                st.session_state.show_answer = False
                                st.rerun()
                        # 区切り線
                        st.markdown("<hr style='margin: 0.3em 0px; border: 0.5px dashed #444;'/>", unsafe_allow_html=True)
            else:
                st.info("まだ学習履歴がありません．")

    # --------------------------------------
    # モード：成績リスト（年度別）＆ リセット復元
    # --------------------------------------
    elif st.session_state.mode == "dashboard":
        st.markdown("<h1 style='text-align: center;'>成績リスト (年度別)</h1>", unsafe_allow_html=True)
        st.info("**【凡例】** 🟢: 完璧 (〇) ｜ 🟡: だいたい解けた (△) ｜ 🟠: 少し解けた (▲) ｜ 🔴: わからなかった (×) ｜ ⚪: 未評価")
        
        col_title, col_reset = st.columns([6, 4])
        with col_title:
            st.write("年度ごとのボタンから，その年の通し演習や特定の分野のみの演習を直接スタートできます．")
        with col_reset:
            with st.expander("🚨 データのリセット", expanded=False):
                if st.button("📄 評価 (〇▲×) のみリセット", use_container_width=True):
                    current_user = st.session_state.get("username", "Guest")
                    evals = load_evals(current_user)
                    for g in evals:
                        for k in evals[g]:
                            if isinstance(evals[g][k], dict):
                                evals[g][k]["rating"] = ""
                            else:
                                evals[g][k] = {"rating": "", "tags": []}
                    save_evals(evals)
                    st.success("評価のみをリセットしました．")
                    st.rerun()
                
        current_user = st.session_state.get("username", "Guest")
        evals = load_evals(current_user)
        
        # すべての年度を抽出して降順にソート
        all_years = set()
        for qs in data.values():
            for q in qs:
                all_years.add(str(q.get('year', '')))
        sorted_years = sorted(list(all_years), key=extract_year_val, reverse=True)
        
        for year in sorted_years:
            y_qs = []
            for g in GENRE_ORDER:
                if g in data:
                    for q in data[g]:
                        if str(q.get('year', '')) == year:
                            y_qs.append((g, q))
            
            if not y_qs: continue
            
            # 変更後（未評価のカウントなどを行っているブロック）
            counts = {"〇": 0, "△": 0, "▲": 0, "×": 0, "未": 0}
            for g, q in y_qs:
                q_key = f"{q.get('year', '')}_{q.get('number', '')}"
                rating_data = evals.get(g, {}).get(q_key)
                if rating_data is None: r = ""
                elif isinstance(rating_data, str): r = rating_data
                else: r = rating_data.get("rating", "")
                
                if r == "〇": counts["〇"] += 1
                elif r == "△": counts["△"] += 1
                elif r == "▲": counts["▲"] += 1
                elif r == "×": counts["×"] += 1
                else: counts["未"] += 1
            
            total_y = len(y_qs)
            p_maru = int((counts["〇"] / total_y) * 100) if total_y > 0 else 0
            p_san_light = int((counts["△"] / total_y) * 100) if total_y > 0 else 0
            p_sankaku = int((counts["▲"] / total_y) * 100) if total_y > 0 else 0
            p_batsu = int((counts["×"] / total_y) * 100) if total_y > 0 else 0
            
            with st.expander(f"📚 {year} (全{total_y}問) ｜ 達成率: 🟢 {p_maru}%  🟡 {p_san_light}%  🟠 {p_sankaku}%  🔴 {p_batsu}%", expanded=True):
                
                st.write("**🔽 この年度の演習を開始する**")
                btn_cols = st.columns(4)
                
                if btn_cols[0].button("🐈💨 年度全体を通しで解く", key=f"play_all_{year}", use_container_width=True):
                    st.session_state.seq_list = [{"genre": g, "q": q} for g, q in y_qs]
                    st.session_state.seq_idx = 0
                    st.session_state.quiz_mode = "sequential"
                    st.session_state.current_genre = st.session_state.seq_list[0]["genre"]
                    st.session_state.current_q = st.session_state.seq_list[0]["q"]
                    st.session_state.mode = "quiz"
                    st.session_state.show_answer = False
                    st.rerun()
                
                for idx, g_name in enumerate(GENRE_ORDER):
                    with btn_cols[idx+1]:
                        g_qs = [item for item in y_qs if item[0] == g_name]
                        if g_qs:
                            if st.button(f"📘 {g_name}のみ", key=f"play_{year}_{g_name}", use_container_width=True):
                                st.session_state.seq_list = [{"genre": g, "q": q} for g, q in g_qs]
                                st.session_state.seq_idx = 0
                                st.session_state.quiz_mode = "sequential"
                                st.session_state.current_genre = st.session_state.seq_list[0]["genre"]
                                st.session_state.current_q = st.session_state.seq_list[0]["q"]
                                st.session_state.mode = "quiz"
                                st.session_state.show_answer = False
                                st.rerun()
                        else:
                            st.button(f"📘 {g_name} (なし)", key=f"play_none_{year}_{g_name}", disabled=True, use_container_width=True)
                
                st.markdown("<hr style='margin: 0.5em 0px; border: 1px solid #555;'/>", unsafe_allow_html=True)
                
                p_nums = sorted(list(set([str(q.get('number', '')) for _, q in y_qs])), key=extract_num)
                col_widths = [1.5] + [1] * len(p_nums)
                header_cols = st.columns(col_widths)
                
                header_cols[0].markdown("<div style='text-align: center; color: #888;'><b>分野</b></div>", unsafe_allow_html=True)
                for i, p_num in enumerate(p_nums):
                    header_cols[i+1].markdown(f"<div style='text-align: center; color: #888;'><b>{p_num}</b></div>", unsafe_allow_html=True)
                
                st.markdown("<hr style='margin: 0.5em 0px; border: 1px solid #555;'/>", unsafe_allow_html=True)
                
                for g_name in GENRE_ORDER:
                    if not any(g == g_name for g, _ in y_qs):
                        continue
                        
                    row_cols = st.columns(col_widths)
                    row_cols[0].markdown(f"<div style='padding-top: 10px; font-weight: bold;'>{g_name}</div>", unsafe_allow_html=True)
                    
                    for i, p_num in enumerate(p_nums):
                        q_match = next((q for g, q in y_qs if g == g_name and str(q.get('number', '')) == p_num), None)
                        
                        with row_cols[i+1]:
                            if q_match:
                                q_key = f"{q_match.get('year', '')}_{q_match.get('number', '')}"
                                rating_data = evals.get(g_name, {}).get(q_key)
                                
                                if rating_data is None: rating, tags = "", q_match.get("tags", [])
                                elif isinstance(rating_data, str): rating, tags = rating_data, q_match.get("tags", [])
                                else: rating, tags = rating_data.get("rating", ""), rating_data.get("tags", [])
                                
                                rating_icons = {"〇": "🟢", "△": "🟡", "▲": "🟠", "×": "🔴"}
                                btn_label = rating_icons.get(rating, "⚪")
                                tooltip_text = f"タグ: {', '.join(tags)}" if tags else "タグなし"
                                
                                if st.button(btn_label, key=f"btn_matrix_{year}_{g_name}_{q_key}", use_container_width=True, help=tooltip_text):
                                    st.session_state.mode = "quiz"
                                    st.session_state.quiz_mode = "random"
                                    st.session_state.current_genre = g_name
                                    st.session_state.current_q = q_match
                                    st.session_state.show_answer = False
                                    st.rerun()
                            else:
                                st.markdown("<div style='text-align: center; color: #555; padding-top: 10px;'>-</div>", unsafe_allow_html=True)
                    st.markdown("<hr style='margin: 0.2em 0px; border: 0.5px dashed #444;'/>", unsafe_allow_html=True)

    # --------------------------------------
    # モード：出題 ＆ AIチャット
    # --------------------------------------
    elif st.session_state.mode == "quiz":

        if st.session_state.quiz_mode == "sequential":
            st.markdown(f"<h1 style='text-align: center;'>🛤️ コース演習 ({st.session_state.seq_idx + 1} / {len(st.session_state.seq_list)}問目)</h1>", unsafe_allow_html=True)
            current_genre = st.session_state.current_genre
        else:
            st.markdown("<h1 style='text-align: center;'>過去問演習 (ランダム/単発)</h1>", unsafe_allow_html=True)
            genre_list = sorted(list(data.keys()), key=get_genre_idx)
            default_idx = genre_list.index(st.session_state.current_genre) if st.session_state.current_genre in genre_list else 0
            
            col_sel1, col_sel2, col_sel3 = st.columns([1, 2, 1])
            with col_sel2:
                current_genre = st.selectbox("分野を選択してください", genre_list, index=default_idx)
            
            if st.session_state.current_q is None or st.session_state.current_genre != current_genre:
                st.session_state.current_genre = current_genre
                st.session_state.current_q = random.choice(data[current_genre])
                st.session_state.show_answer = False

        q = st.session_state.current_q
        q_key = f"{q.get('year', '')}_{q.get('number', '')}"
        
        if st.session_state.chat_q_key != q_key:
            st.session_state.chat_history = []
            st.session_state.chat_q_key = q_key
            st.session_state.ai_generated_answer = None
        
        global_stats = get_global_q_stats()
        unique_q_key = f"{current_genre}_{q_key}"
        diff_ui = get_difficulty_ui(unique_q_key, global_stats)
        
        st.markdown(f"<h3 style='text-align: center;'>【出題】{q.get('year', '')} {current_genre} - {q.get('number', '')} <br><div style='margin-top: 8px;'>{diff_ui}</div></h3>", unsafe_allow_html=True)
        
        if os.path.exists(q.get("question_image", "")):
         # 1. 確実な中央寄せのために「カラム」を使って左右に見えない余白を作る
         # 比率を [2, 6, 1] にすることで、画像がちょうど良いバランスで中央に配置されます
         col_space1, col_img, col_space2 = st.columns([2, 6, 1])
         
         with col_img:
             # カラムの幅いっぱいに広げつつ、CSSで高さを制限する
             st.image(q.get("question_image", ""), use_container_width=True)
         
         # ====================================================
         # 💡 綺麗なUIと装飾専用のCSS
         # ====================================================
         st.markdown("<hr style='margin: 2em 0px 1em 0px; border: 1px solid #444;'/>", unsafe_allow_html=True)
         
         st.markdown("<div style='color: #aaa; font-size: 0.9em; margin-bottom: 10px; text-align: center;'>📷 <b>問題画像</b> ｜ 画像をクリックすると全画面で拡大表示できます．</div>", unsafe_allow_html=True)
         
         st.markdown("<hr style='margin: 1em 0px 2em 0px; border: 1px dashed #444;'/>", unsafe_allow_html=True)

         # 画像の装飾（白背景、角丸、影、高さ制限）だけをCSSで適用する
         st.markdown("""
         <style>
         /* 画像本体の見た目を整え、カラムの中での中央配置を念押しする */
         div[data-testid="stImage"] img {
             display: block !important;
             margin: 0 auto !important;
             max-height: 500px !important; 
             width: auto !important;
             max-width: 100% !important;
             object-fit: contain !important;
             background-color: #ffffff !important;
             padding: 15px !important;
             border-radius: 10px !important;
             box-shadow: 0px 4px 15px rgba(0, 0, 0, 0.3) !important;
         }
         </style>
         """, unsafe_allow_html=True)
        
        if not st.session_state.show_answer:
            st.write("") 
            col_btn1, col_btn2, col_btn3 = st.columns([1, 2, 1])
    
            with col_btn2:
                c1, c2, c3 = st.columns(3)
        
                with c1:
                    if st.button("解答を表示する", type="primary", use_container_width=True): 
                        st.session_state.show_answer = True
                        st.rerun()

                with c2:
                    gen_ai = st.button("🤖 AIで解説生成", use_container_width=True)

                with c3:
                    btn_skip_text = "スキップ ⏭️" if st.session_state.quiz_mode == "random" or st.session_state.seq_idx < len(st.session_state.seq_list) - 1 else "ホームに戻る 🐾"
                    if st.button(btn_skip_text, use_container_width=True):
                        if st.session_state.quiz_mode == "random":
                            st.session_state.current_q = random.choice(data[current_genre])
                            st.session_state.show_answer = False
                            st.rerun()
                        elif st.session_state.quiz_mode == "sequential":
                            st.session_state.seq_idx += 1
                            if st.session_state.seq_idx < len(st.session_state.seq_list):
                                nxt = st.session_state.seq_list[st.session_state.seq_idx]
                                st.session_state.current_genre = nxt["genre"]
                                st.session_state.current_q = nxt["q"]
                                st.session_state.show_answer = False
                                st.rerun()
                            else:
                                st.session_state.just_completed = True
                                st.session_state.mode = "home"
                                st.rerun()

            # --- AIボタンが押された時の処理（列の外に出して広く表示する） ---
            if gen_ai:
                api_key = conf.get("gemini_api_key")
                if not api_key:
                    st.warning("⚠️ 個人設定画面から Gemini API キーを設定してください．")
                else:
                    with st.spinner("AIが全力で解答を作成中です...（十数秒かかる場合があります）"):
                        try:
                            genai.configure(api_key=api_key)
                            model = genai.GenerativeModel('gemini-3.1-flash-lite')
                    
                            ai_prompt = """
                            この問題画像の解答・解説を作成してください．
                            【厳格な記述ルール】
                            ・計算式などはあきらかな場合を除き，できる限り途中式を明示すること．
                            ・文章は日本語とし，句点・句読点は「．」「，」を使用すること．
                            ・数式は必ず LaTeX 形式で記述すること．インライン数式は $，独立した数式ブロックは $$ で囲み，記号と数式の間にスペースを空けないこと．
                            ・段落や数式ブロックの前後には適切に改行を入れること．
                            """
                            img_path = q.get("question_image", "")
                            if img_path and os.path.exists(img_path):
                                from PIL import Image
                                img_obj = Image.open(img_path)
                                
                                response = model.generate_content([ai_prompt, img_obj])
                        
                                # 💡 変更箇所：生成されたテキストをこの問題の「解答」として一時的に上書きし、画面をリロードする
                                st.session_state.ai_generated_answer = response.text
                                st.session_state.show_answer = True
                                st.rerun()
                        
                            else:
                                st.warning("⚠️ 問題の画像が見つからないため，AIに読み込ませることができません．")
                        except Exception as e:
                            st.error(f"エラーが発生しました．1〜2分待ってから再度お試しください．\n\n詳細: {e}")
        
        else:
            st.markdown("---")

            st.markdown("<h3 style='text-align: center;'>【解答・解説】</h3>", unsafe_allow_html=True)
            ans_text = q.get("answer", "")
            if isinstance(ans_text, str): ans_text = ans_text.replace("\n", "\n")

            col_ans1, col_ans2, col_ans3 = st.columns([1, 6, 1])
            with col_ans2:
                # --- ここから書き換え：常にタブを表示して、どちらからでも行き来できるようにする ---
                tab_normal, tab_ai = st.tabs(["📝 通常の解答", "🤖 AI生成解答"])
        
                with tab_normal:
                    st.markdown(ans_text if ans_text else "*（解答がありません）*")
            
                with tab_ai:
                    if st.session_state.get("ai_generated_answer"):
                        # 既にAI解答がある場合は表示
                        st.markdown("【🤖 AI自動生成解答】\n\n" + st.session_state.ai_generated_answer)
                    else:
                        # まだAI解答がない場合は、タブの中に生成ボタンを置く
                        st.info("💡 AIによる解説はまだ生成されていません．")
                        if st.button("✨ 今すぐこの問題の解説をAIに作ってもらう", key=f"gen_ai_tab_{q_key}"):
                            api_key = conf.get("gemini_api_key")
                            if not api_key:
                                st.warning("⚠️ 個人設定画面から Gemini API キーを設定してください．")
                            else:
                                with st.spinner("AIが全力で解答を作成中です...（十数秒かかる場合があります）"):
                                    try:
                                        genai.configure(api_key=api_key)
                                        model = genai.GenerativeModel('gemini-3.1-flash-lite')
                                
                                        ai_prompt = """
                                        この問題画像の解答・解説を作成してください．
                                        【厳格な記述ルール】
                                        ・計算式などはあきらかな場合を除き，できる限り途中式を明示すること．
                                        ・文章は日本語とし，句点・句読点は「．」「，」を使用すること．
                                        ・数式は必ず LaTeX 形式で記述すること．インライン数式は $，独立した数式ブロックは $$ で囲み，記号と数式の間にスペースを空けないこと．
                                        ・段落や数式ブロックの前後には適切に改行を入れること．
                                        """
                                        img_path = q.get("question_image", "")
                                        if img_path and os.path.exists(img_path):
                                            from PIL import Image
                                            img_obj = Image.open(img_path)
                                            
                                            response = model.generate_content([ai_prompt, img_obj])
                                            
                                            st.session_state.ai_generated_answer = response.text
                                            st.rerun() # 画面を更新してタブ内にAI解答を表示
                                        else:
                                            st.warning("⚠️ 問題の画像が見つからないため，AIに読み込ませることができません．")
                                    except Exception as e:
                                        st.error(f"エラーが発生しました．1〜2分待ってから再度お試しください．\n\n詳細: {e}")
            
            st.markdown("---")
            st.markdown("<h3 style='text-align: center;'>💬 AI家庭教師に質問する</h3>", unsafe_allow_html=True)
            
            if not conf.get("gemini_api_key"):
                st.warning("AI家庭教師を利用するには、「⚙️ 個人設定」からGemini APIキーを設定してください。")
            else:
                col_chat1, col_chat2, col_chat3 = st.columns([1, 6, 1])
                with col_chat2:
                    for chat in st.session_state.chat_history:
                        if chat["role"] == "user":
                            st.markdown(f"**あなた:** {chat['text']}")
                        else:
                            st.markdown(f"**AI先生:** {chat['text']}")
                            
                    with st.form("chat_form", clear_on_submit=True):
                        user_input = st.text_input("この問題について分からない部分を質問してください:")
                        submitted = st.form_submit_button("質問する 🐈💨")
                        
                        if submitted and user_input:
                            st.session_state.chat_history.append({"role": "user", "text": user_input})
                            
                            with st.spinner("AI先生が考え中..."):
                                try:
                                    # エラー対策：最新モデルを指定し、失敗時は予備モデルを使用
                                    model = genai.GenerativeModel('gemini-3.1-flash-lite')
                                        
                                    context_prompt = f"あなたは親切な大学の先生です。以下の過去問の解答について生徒から質問が来ました。\n\n【解答解説】\n{ans_text}\n\n【生徒からの質問】\n{user_input}\n\n数式はLaTeX形式で、句読点は「．」「，」を使用し、生徒が理解できるように分かりやすく教えてください。"
                                    
                                    if os.path.exists(q.get("question_image", "")):
                                        img = Image.open(q.get("question_image", ""))
                                        response = model.generate_content([context_prompt, img])
                                    else:
                                        response = model.generate_content(context_prompt)
                                        
                                    st.session_state.chat_history.append({"role": "ai", "text": response.text})
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"AIとの通信中にエラーが発生しました: {e}")

            st.markdown("---")
            
            st.markdown("---")
            with st.expander("⚠️ 解答に間違いを見つけたら...", expanded=False):
                st.write("解答や解説に誤りがある場合，ここから管理者に報告できます．")
                with st.form(key=f"correction_form_{q_key}", clear_on_submit=True):
                    report_text = st.text_area("どの部分が間違っているか，詳細を教えてください．")
                    submit_report = st.form_submit_button("修正依頼を送信する 📤")
                    
                    if submit_report:
                        if report_text.strip():
                            # Firebaseの correction_requests に保存
                            req_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + st.session_state.get("username", "Guest")
                            req_data = {
                                "genre": current_genre,
                                "year": q.get('year', ''),
                                "number": q.get('number', ''),
                                "detail": report_text,
                                "reported_by": st.session_state.get("username", "Guest"),
                                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            db.reference(f'app_data/correction_requests/{req_id}').set(req_data)
                            st.success("報告ありがとうございます！管理者に修正依頼を送信しました．")
                        else:
                            st.error("詳細を入力してください．")

            st.markdown("<h3 style='text-align: center;'>自己評価 ＆ タグ付け</h3>", unsafe_allow_html=True)

            # 💡 追加：ラジオボタンを巨大化して押しやすくする魔法のCSS
            st.markdown("""
            <style>
            div[role="radiogroup"] > label {
                padding: 10px 15px !important;
                background-color: rgba(255, 255, 255, 0.05) !important;
                border-radius: 10px !important;
                border: 1px solid #444 !important;
                margin-right: 5px !important;
                cursor: pointer !important;
                transition: all 0.2s ease !important;
            }
            div[role="radiogroup"] > label:hover {
                background-color: rgba(255, 255, 255, 0.1) !important;
                border-color: #FF69B4 !important;
            }
            </style>
            """, unsafe_allow_html=True)

            current_user = st.session_state.get("username", "Guest")
            evals = load_evals(current_user)
            rating_data = evals.get(current_genre, {}).get(q_key)
            if rating_data is None: rating, current_tags = "", q.get("tags", [])
            elif isinstance(rating_data, str): rating, current_tags = rating_data, q.get("tags", [])
            else: rating, current_tags = rating_data.get("rating", ""), rating_data.get("tags", [])

            col_eval1, col_eval2, col_eval3 = st.columns([1, 6, 1]) # 幅を広げてボタンを配置しやすく
            with col_eval2:
                if current_tags:
                    st.write("🏷️ **登録済みのタグ：**")
                    render_beautiful_tags(current_tags)
                
                # 💡 修正：絵文字を入れて視覚的に分かりやすく、大きなボタンにします
                options = ["⚪ 未選択", "🟢 完璧", "🟡 だいたい解けた", "🟠 要復習", "🔴 わからなかった"]
                if rating == "〇": default_radio_idx = 1
                elif rating == "△": default_radio_idx = 2
                elif rating == "▲": default_radio_idx = 3
                elif rating == "×": default_radio_idx = 4
                else: default_radio_idx = 0
                
                selected_rating = st.radio("この問題の理解度は？", options, index=default_radio_idx, horizontal=True)

                current_tags_str = ", ".join(current_tags)
                input_tags_str = st.text_input("🏷️ 新しいタグを追加（カンマ区切りで入力）", value=current_tags_str)
                
                st.write("")
                btn_text = "評価とタグを保存して次の問題へ" if st.session_state.quiz_mode == "random" or st.session_state.seq_idx < len(st.session_state.seq_list) - 1 else "評価を保存してコース完了！"
                
                if st.button(btn_text, type="primary", use_container_width=True):
                    # 💡 追加：未選択のまま押したら、真っ赤なエラーを出してここで処理をストップさせる！
                    if selected_rating == "⚪ 未選択":
                        st.error("🚨 【ストップ！】評価が「未選択」です！集計データを作るために、どれか1つを選んでから保存してください。")
                        st.stop() # ここでプログラムを強制停止するので、絶対に次へ進めません

                    if current_genre not in evals: evals[current_genre] = {}
                    
                    normalized_tags_str = input_tags_str.replace("，", ",")
                    new_tags = [t.strip() for t in normalized_tags_str.split(",") if t.strip()]
            
                    import time
                    final_rating = ""
                    if "完璧" in selected_rating: final_rating = "〇"
                    elif "だいたい" in selected_rating: final_rating = "△"
                    elif "要復習" in selected_rating: final_rating = "▲"
                    elif "わからなかった" in selected_rating: final_rating = "×"
                    
                    eval_data = {
                        "rating": final_rating, 
                        "tags": new_tags,
                        "timestamp": time.time()
                    }
        
                    evals[current_genre][q_key] = eval_data
                    update_single_eval(current_genre, q_key, eval_data)
                    st.toast("✨ 評価とタグを保存しました！", icon="🎉")

                    # 次の問題への遷移処理
                    if st.session_state.quiz_mode == "random":
                        st.session_state.current_q = random.choice(data[current_genre])
                        st.session_state.show_answer = False
                        st.rerun()
                    elif st.session_state.quiz_mode == "sequential":
                        st.session_state.seq_idx += 1
                        if st.session_state.seq_idx < len(st.session_state.seq_list):
                            nxt = st.session_state.seq_list[st.session_state.seq_idx]
                            st.session_state.current_genre = nxt["genre"]
                            st.session_state.current_q = nxt["q"]
                            st.session_state.show_answer = False
                            st.rerun()
                        else:
                            st.session_state.just_completed = True
                            st.session_state.mode = "home"
                            st.rerun()