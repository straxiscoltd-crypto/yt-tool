"""
YouTube 一括文字起こし & キーワード調査ツール
------------------------------------------------
起動:  streamlit run app.py
"""
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime

import pandas as pd
import streamlit as st
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# ============================================================
# ユーティリティ
# ============================================================
VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str):
    url = url.strip()
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def sec_to_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@st.cache_data(show_spinner=False, ttl=3600)
def get_metadata(video_id: str) -> dict:
    opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return {
        "video_id": video_id,
        "title": info.get("title", ""),
        "channel": info.get("channel") or info.get("uploader", ""),
        "upload_date": info.get("upload_date", ""),
        "duration": info.get("duration") or 0,
        "view_count": info.get("view_count") or 0,
        "like_count": info.get("like_count") or 0,
        "comment_count": info.get("comment_count") or 0,
        "tags": info.get("tags") or [],
        "categories": info.get("categories") or [],
        "description": info.get("description") or "",
        "chapters": [c.get("title") for c in (info.get("chapters") or []) if c.get("title")],
    }


@st.cache_data(show_spinner=False, ttl=3600)
def get_transcript(video_id: str, langs: tuple):
    """(segments, language_code, is_generated) を返す。取れなければ例外。"""
    api = YouTubeTranscriptApi()
    tlist = api.list(video_id)
    try:
        tr = tlist.find_transcript(list(langs))
    except Exception:
        # 希望言語がなければ、手動→自動の順で最初に見つかったものを翻訳して使う
        first = next(iter(tlist))
        try:
            tr = first.translate(langs[0])
        except Exception:
            tr = first
    fetched = tr.fetch()
    segs = [{"start": s.start, "duration": s.duration, "text": s.text} for s in fetched]
    return segs, tr.language_code, tr.is_generated


@st.cache_data(show_spinner=False, ttl=3600)
def youtube_suggest(query: str, hl: str = "ja") -> list:
    """YouTube検索窓のサジェスト（実際に検索されている関連クエリ）"""
    url = (
        "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt"
        f"&hl={hl}&q={urllib.parse.quote(query)}"
    )
    try:
        r = urllib.request.urlopen(url, timeout=10)
        ctype = r.headers.get("Content-Type", "")
        enc = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype)
        if m:
            enc = m.group(1)
        data = json.loads(r.read().decode(enc, errors="replace"))
        return data[1] if len(data) > 1 else []
    except Exception:
        return []


JA_STOP = set("こと もの ため よう これ それ あれ ところ とき ので から まで など です ます ある いる する なる れる られる".split())
EN_STOP = set("the a an and or of to in on for with is are was were be been it this that at by from as you your we our i my not but if then so do does have has had can will just like".split())


def extract_keywords(text: str, top_n: int = 25) -> list:
    """簡易キーワード抽出（英数字語 + 日本語の漢字/カタカナ連続）"""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}|[一-龥々]{2,}|[ァ-ヴー]{3,}", text)
    c = Counter()
    for t in tokens:
        tl = t.lower()
        if tl in EN_STOP or t in JA_STOP:
            continue
        c[t] += 1
    return c.most_common(top_n)


def build_txt(meta: dict, segs, lang, generated, with_ts: bool) -> str:
    lines = [
        f"# {meta['title']}",
        f"URL: https://www.youtube.com/watch?v={meta['video_id']}",
        f"チャンネル: {meta['channel']}  / 公開日: {meta['upload_date']}  / 再生数: {meta['view_count']:,}",
        f"字幕言語: {lang} ({'自動生成' if generated else '手動'})",
        "",
    ]
    if with_ts:
        lines += [f"[{sec_to_ts(s['start'])}] {s['text']}" for s in segs]
    else:
        lines.append(" ".join(s["text"] for s in segs))
    return "\n".join(lines)


# ============================================================
# パスワード認証（リンクを知っている人だけに限定）
# ============================================================
def check_password() -> bool:
    """secrets.toml の app_password と一致したときだけ True を返す。"""
    correct = st.secrets.get("app_password", None)
    if not correct:          # パスワード未設定ならそのまま通す（ローカル実行用）
        return True
    if st.session_state.get("authed"):
        return True

    st.title("🔒 ログイン")
    st.caption("このツールを使うにはパスワードが必要です。")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン", type="primary"):
        if pw == correct:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    return False


# ============================================================
# UI
# ============================================================
st.set_page_config(page_title="YouTube一括文字起こし＆キーワード調査", page_icon="🎬", layout="wide")

if not check_password():
    st.stop()

st.title("🎬 YouTube 一括文字起こし & キーワード調査")

with st.sidebar:
    st.header("設定")
    lang_pref = st.multiselect(
        "字幕の優先言語（上から順に試す）",
        ["ja", "en", "ko", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ja", "en"],
    )
    with_ts = st.checkbox("タイムスタンプ付きで出力", value=True)
    do_suggest = st.checkbox("YouTubeサジェスト（関連検索クエリ）を取得", value=True)
    suggest_hl = st.selectbox("サジェストの言語/地域", ["ja", "en"], index=0)
    top_n = st.slider("抽出キーワード数", 10, 50, 25)
    st.caption(
        "※ どの検索ワードで実際に表示されたか（正確な流入クエリ）は動画オーナーの"
        "YouTube Studioでしか見られません。ここでは タグ・タイトル・説明文・字幕の"
        "キーワードと、YouTubeサジェストから「狙っているクエリ」を推定します。"
    )

urls_text = st.text_area(
    "YouTubeのURLを貼り付け（1行に1本、何本でもOK）",
    height=160,
    placeholder="https://www.youtube.com/watch?v=xxxxxxxxxxx\nhttps://youtu.be/yyyyyyyyyyy\nhttps://www.youtube.com/shorts/zzzzzzzzzzz",
)

run = st.button("▶ 一括実行", type="primary", use_container_width=True)

if run:
    ids = []
    for line in urls_text.splitlines():
        vid = extract_video_id(line)
        if vid and vid not in ids:
            ids.append(vid)
    if not ids:
        st.error("有効なYouTube URLが見つかりませんでした。")
        st.stop()

    results = []
    prog = st.progress(0, text="処理中…")
    for i, vid in enumerate(ids):
        prog.progress((i) / len(ids), text=f"{i+1}/{len(ids)}  {vid} を処理中…")
        item = {"video_id": vid, "error": None}
        try:
            item["meta"] = get_metadata(vid)
        except Exception as e:
            item["meta"] = {"video_id": vid, "title": vid, "channel": "", "upload_date": "",
                            "duration": 0, "view_count": 0, "like_count": 0, "comment_count": 0,
                            "tags": [], "categories": [], "description": "", "chapters": []}
            item["error"] = f"メタ情報取得失敗: {e}"
        try:
            segs, lang, gen = get_transcript(vid, tuple(lang_pref or ["ja", "en"]))
            item.update(segs=segs, lang=lang, generated=gen)
        except Exception as e:
            item.update(segs=[], lang="", generated=False)
            msg = str(e).splitlines()[0]
            item["error"] = (item["error"] + " / " if item["error"] else "") + f"字幕取得失敗: {msg}"

        m = item["meta"]
        full_text = " ".join(s["text"] for s in item["segs"])
        item["keywords"] = extract_keywords(
            m["title"] + " " + m["description"] + " " + " ".join(m["tags"]) + " " + full_text, top_n
        )
        # サジェスト：タイトルを軸に、上位キーワードでも引く
        item["suggest"] = {}
        if do_suggest:
            seeds = [m["title"][:40]] + [k for k, _ in item["keywords"][:3]]
            for s in seeds:
                if s.strip():
                    item["suggest"][s] = youtube_suggest(s, suggest_hl)
        results.append(item)
    prog.progress(1.0, text="完了")
    st.session_state["results"] = results
    st.session_state["with_ts"] = with_ts

# ============================================================
# 結果表示
# ============================================================
if "results" in st.session_state:
    results = st.session_state["results"]
    with_ts = st.session_state["with_ts"]

    # --- サマリー表 ---
    st.subheader("📋 サマリー")
    rows = []
    for r in results:
        m = r["meta"]
        rows.append({
            "タイトル": m["title"],
            "チャンネル": m["channel"],
            "公開日": m["upload_date"],
            "長さ": sec_to_ts(m["duration"]),
            "再生数": m["view_count"],
            "高評価": m["like_count"],
            "字幕": f"{r['lang']}{'(自動)' if r['generated'] else ''}" if r["segs"] else "なし",
            "文字数": len("".join(s["text"] for s in r["segs"])),
            "タグ数": len(m["tags"]),
            "エラー": r["error"] or "",
            "URL": f"https://www.youtube.com/watch?v={m['video_id']}",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # --- 一括ダウンロード ---
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            m = r["meta"]
            safe = re.sub(r'[\\/:*?"<>|]', "_", m["title"])[:60] or m["video_id"]
            if r["segs"]:
                zf.writestr(f"{safe}_{m['video_id']}.txt",
                            build_txt(m, r["segs"], r["lang"], r["generated"], with_ts))
            kw = {
                "video_id": m["video_id"], "title": m["title"], "tags": m["tags"],
                "chapters": m["chapters"], "keywords": r["keywords"], "suggest": r["suggest"],
            }
            zf.writestr(f"{safe}_{m['video_id']}_keywords.json",
                        json.dumps(kw, ensure_ascii=False, indent=2))
        zf.writestr("summary.csv", df.to_csv(index=False))
    c1, c2 = st.columns(2)
    c1.download_button("📦 全部まとめてZIPでダウンロード", zbuf.getvalue(),
                       file_name=f"youtube_batch_{datetime.now():%Y%m%d_%H%M}.zip",
                       mime="application/zip", use_container_width=True)
    c2.download_button("📄 サマリーCSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name="summary.csv", mime="text/csv", use_container_width=True)

    # --- 各動画の詳細 ---
    st.subheader("🎥 動画ごとの詳細")
    for r in results:
        m = r["meta"]
        with st.expander(f"{m['title']}  —  {m['channel']}"):
            if r["error"]:
                st.warning(r["error"])
            t1, t2, t3 = st.tabs(["📝 文字起こし", "🔑 キーワード・タグ", "🔍 検索クエリ（サジェスト）"])
            with t1:
                if r["segs"]:
                    txt = build_txt(m, r["segs"], r["lang"], r["generated"], with_ts)
                    st.download_button("このテキストをダウンロード", txt,
                                       file_name=f"{m['video_id']}.txt", key=f"dl_{m['video_id']}")
                    st.text_area("", txt, height=400, key=f"ta_{m['video_id']}", label_visibility="collapsed")
                else:
                    st.info("字幕が取得できませんでした（字幕なし／非公開／IP制限など）。")
            with t2:
                cA, cB = st.columns(2)
                with cA:
                    st.markdown("**設定タグ（投稿者が付けたもの）**")
                    st.write(", ".join(m["tags"]) if m["tags"] else "なし")
                    st.markdown("**カテゴリ**")
                    st.write(", ".join(m["categories"]) or "—")
                    if m["chapters"]:
                        st.markdown("**チャプター**")
                        st.write("\n".join(f"- {c}" for c in m["chapters"]))
                with cB:
                    st.markdown("**頻出キーワード（タイトル+説明文+タグ+字幕）**")
                    st.dataframe(pd.DataFrame(r["keywords"], columns=["キーワード", "出現回数"]),
                                 hide_index=True, use_container_width=True)
                st.markdown("**説明文**")
                st.text(m["description"][:2000])
            with t3:
                if r["suggest"]:
                    for seed, sugg in r["suggest"].items():
                        st.markdown(f"**「{seed}」で検索したときのサジェスト**")
                        st.write("、".join(sugg) if sugg else "—")
                else:
                    st.info("サジェスト取得はオフです。")
