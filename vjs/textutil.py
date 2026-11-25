"""Language tagging, tokenization, pinyin, and the journal's house style.

House style (from the journal handoff):
  Full Chinese sentence  →  "句子。" *(English translation only)*
  Code-switched word     →  汉字 *(pīnyīn / English meaning)*
  Annotation             →  *[In square brackets, italic.]*
  Speakers               →  👧 👨 👩
"""
import re

CJK = re.compile(r"[㐀-鿿豈-﫿]")
CJK_RUN = re.compile(r"[㐀-鿿豈-﫿]+(?:[，、][㐀-鿿豈-﫿]+)*")
LATIN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
ZH_PUNCT = "，。！？、；：“”‘’（）《》…—·～"

_jieba = None


def _jb():
    global _jieba
    if _jieba is None:
        import logging
        import jieba
        jieba.setLogLevel(logging.WARNING)
        _jieba = jieba
    return _jieba


def lang_of(text):
    zh = len(CJK.findall(text or ""))
    en = len(LATIN_WORD.findall(text or ""))
    if zh and en:
        return "mixed"
    if zh:
        return "zh"
    if en:
        return "en"
    return "other"


def tokens(text):
    """[(token, 'zh'|'en')] — jieba words for Chinese, lowercase words for English."""
    out = []
    text = re.sub(r"\{[^{}]*\}", " ", SOUND.sub(" ", text or ""))  # sounds / notes aren't words she said
    for part in re.split(r"([㐀-鿿豈-﫿]+)", text or ""):
        if not part:
            continue
        if CJK.match(part):
            out += [(w, "zh") for w in _jb().lcut(part) if w.strip() and w not in ZH_PUNCT]
        else:
            out += [(w.lower(), "en") for w in LATIN_WORD.findall(part)]
    return out


def _split_long(w):
    """晒太阳 → 晒 太阳, 累累的 → 累累 的 (matches the journal's pinyin spacing)."""
    if len(w) < 3:
        return [w]
    subs = {s for s in _jb().cut_for_search(w) if s != w and len(s) >= 2}
    if w[1:] in subs:
        return [w[0], w[1:]]
    if w[:-1] in subs:
        return [w[:-1], w[-1]]
    return [w]


_py_ready = False
# Baby-talk / household readings the dictionaries get wrong. Extend via
# "pinyin_overrides" in config.local.json.
PINYIN_OVERRIDES = {"累累": "lèilèi", "东西": "dōngxi", "觉觉": "jiàojiào", "饭饭": "fànfàn", "水水": "shuǐshuǐ"}


def pinyin(chinese):
    global _py_ready
    from pypinyin import Style, pinyin as _py
    if not _py_ready:
        from pypinyin_dict.phrase_pinyin_data import cc_cedict  # neutral tones: yīfu, pìgu, péngyou
        cc_cedict.load()
        from .config import CFG
        PINYIN_OVERRIDES.update(CFG.get("pinyin_overrides", {}))
        _py_ready = True
    words = [p for w in _jb().lcut(chinese) for p in _split_long(w)]
    res = []
    for w in words:
        if not CJK.search(w):
            continue
        res.append(PINYIN_OVERRIDES.get(w) or "".join(x[0] for x in _py(w, style=Style.TONE, heteronym=False)))
    return " ".join(res)


def cjk_runs(text):
    return [m.group(0) for m in CJK_RUN.finditer(text or "")]


def is_mostly_chinese(text):
    zh = len(CJK.findall(text or ""))
    en = len(LATIN_WORD.findall(text or ""))
    return zh > 0 and en <= max(1, zh // 6)


# Whisper sometimes answers in Traditional characters (機器人). The journal is Simplified throughout, so unify on the way in.
_T2S_PAIRS = ("機机 愛爱 們们 這这 個个 來来 說说 對对 時时 會会 學学 習习 邊边 還还 過过 馬马 媽妈 臉脸 見见 開开 關关 門门 問问 間间 "
              "隨随 風风 飛飞 龍龙 魚鱼 鳥鸟 雞鸡 貓猫 蟲虫 點点 實实 現现 當当 歲岁 國国 書书 買买 賣卖 長长 電电 話话 聽听 讀读 "
              "寫写 認认 識识 進进 選选 運运 動动 歡欢 樂乐 請请 謝谢 給给 讓让 從从 兩两 幾几 麼么 樣样 為为 萬万 與与 總总 "
              "裡里 麵面 頭头 頸颈 腳脚 襪袜 褲裤 帶带 蘋苹 飯饭 湯汤 覺觉 戲戏 遊游 畫画 車车 鐘钟 錶表 錢钱 難难 題题 誰谁 "
              "隻只 條条 張张 塊块 顆颗 雙双 樹树 葉叶 藍蓝 綠绿 顏颜 蝦虾 鴨鸭 豬猪 號号 氣气 紅红 鐵铁 養养 體体 鬧闹 煩烦 "
              "嚇吓 嘗尝 醫医 藥药 親亲 喲哟 擔担 沒没 嗎吗 蘿萝 蔔卜 餅饼 餃饺 餛馄 飩饨 區区 舊旧 較较 讚赞 畢毕 響响 壞坏 "
              "擠挤 髒脏 靜静 聲声 願愿 應应 該该 離离 經经 務务 驚惊 嚴严 補补 幫帮 試试 爺爷 護护 場场 師师 園园 擺摆 傘伞 "
              "滿满 溫温 熱热 燈灯 燒烧 爐炉 牆墙 鏡镜 針针 鉛铅 筆笔 紙纸 貝贝 寶宝 擇择 週周 禮礼 無无 衛卫 噴喷 嗚呜 兒儿 "
              "臺台 檯台 鍵键 隊队 結结 繼继 續续 絲丝 線线 級级 細细 組组 終终 練练 彈弹 擊击 戰战 勝胜 負负 贏赢 輸输 麗丽 "
              "準准 備备 紹绍 訴诉 講讲 謊谎 誤误 調调 譯译 詞词 語语 賽赛 賞赏 錯错 鎖锁 鑰钥 爲为 咱咱 妳你 祢你 衹只 麽么 "
              "勞劳 動动 歷历 圖图 團团 壓压 嬰婴 夢梦 對对 專专 將将 傷伤 兇凶 剛刚 劇剧 勵励 啟启 喚唤 嘆叹 嚐尝 塗涂 墳坟 "
              "婦妇 孫孙 寵宠 尋寻 導导 屆届 嶼屿 帥帅 幣币 廚厨 廳厅 彎弯 徹彻 悶闷 愛爱 憂忧 懶懒 戲戏 撥拨 擁拥 敵敌 數数 "
              "斷断 於于 旣既 暫暂 曬晒 東东 構构 樓楼 橋桥 檢检 欄栏 歸归 歲岁 殘残 毀毁 氫氢 洩泄 淨净 渴渴 湧涌 滅灭 澀涩")
T2S = {p[0]: p[1] for p in _T2S_PAIRS.split() if len(p) == 2 and p[0] != p[1]}


def to_simplified(text):
    return "".join(T2S.get(c, c) for c in text) if text else text


_FW = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；", ".": "。"}


def zh_punct(text):
    """ASCII punctuation right after a Chinese character → full-width (journal style)."""
    return re.sub(r"(?<=[\u3400-\u9fff])\s*([,?!:;.])(?!\d)", lambda m: _FW[m.group(1)], text or "")


# sounds / actions / stage directions — any bracket style you happen to type (the Chinese keyboard turns < into 《):
# [giggles]  <lots of roaring>  《roaring》  《roaring>  【laughs】  (whispers)  （哭）  *sighs*
SOUND = re.compile(r"\[([^\[\]]{1,60})\]|\*([^*]{1,60})\*|[<＜《〈]([^<>＜＞《》〈〉]{1,60})[>＞》〉]"
                   r"|【([^【】]{1,60})】|[(（]([^()（）]{1,60})[)）]")


_SOUND_LEADS = {"the", "a", "says", "say", "said", "hear", "heard", "hears", "goes", "go", "went", "called", "call", "sound", "word"}


def _sound_text(m):
    """[Ding-ding-ding] and *ding ding ding* are the same sound: lower-case, hyphens → spaces, so every card spells it one way."""
    t = next(g for g in m.groups() if g is not None).strip()
    return re.sub(r"\s+", " ", re.sub(r"(?<=\w)[-–—‐](?=\w)", " ", t)).lower()


def speech_only(text):
    """Text without [sounds] / *actions* / {notes} — what gets translated."""
    return re.sub(r"\s{2,}", " ", SOUND.sub(" ", re.sub(r"\{[^{}]*\}", " ", text or ""))).strip()


_ASCII_P = {"。": ".", "，": ",", "！": "!", "？": "?", "：": ":", "；": ";"}


def _zh_sentence(text):
    """A Chinese sentence (rendered as 汉字 + English translation) — not a short code-switch like "Goodbye 机器人" or "这是我的 letters"."""
    en, zh = len(LATIN_WORD.findall(text or "")), len(CJK.findall(text or ""))
    return is_mostly_chinese(text) and (en == 0 or zh >= 6)


def _space_scripts(text):
    """A space where Latin and Chinese meet: "pick了这个princess妈妈" → "pick 了这个 princess 妈妈"."""
    return re.sub(r"(?<=[A-Za-z])(?=[㐀-鿿])|(?<=[㐀-鿿])(?=[A-Za-z])", " ", text)


_GLOSS = re.compile(r"(\*\((?:[^()]|\([^()]*\))*\)\*|\*\[[^\]]*\]\*)")


def fix_rendered(s):
    """The same clean-ups applied to an already-rendered journal line (glosses and annotations are left untouched), so cards
    rendered before a rule existed pick it up without losing hand-edited glosses. Idempotent."""
    if not s:
        return s
    parts = _GLOSS.split(to_simplified(s))
    plain = "".join(p for i, p in enumerate(parts) if i % 2 == 0)
    plain = re.sub(r'^[^"]*"', "", plain)
    if CJK.search(plain) and not _zh_sentence(plain):
        out = []
        for i, p in enumerate(parts):
            if i % 2:
                out.append(p)
                continue
            p = re.sub(r"[。.]{3,}|…+", "...", p)
            p = re.sub(r"\s*([。，！？：；])\s*", lambda m: _ASCII_P[m.group(1)] + " ", p)
            p = _space_scripts(p)
            out.append(p)
        s = "".join(out)
        s = re.sub(r"(\)\*)(?=[A-Za-z㐀-鿿])", r"\1 ", s)
        s = re.sub(r"\s+([.,!?:;])", r"\1", s)
    elif not CJK.search(plain):
        s = re.sub(r"\s*([。，！？])", lambda m: _ASCII_P[m.group(1)], s)
    s = re.sub(r'\s+"(?=\s*(?:\*\(|$))', '"', s)          # no space before the closing quote
    s = re.sub(r"\*\[([^\]]*)\]\*", lambda m: "*[" + re.sub(r"\s+", " ", re.sub(r"(?<=\w)[-–—‐](?=\w)", " ", m.group(1))).lower() + "]*", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _render_speech(emoji, text, translation="", glosses=None):
    return _render_line_core(emoji, text, translation, glosses)


def _shorthand(text):
    """the owner's typing shorthand from config.local.json ("shorthand": {"xkx": "<hanzi>"}) expanded as whole words, e.g. an initials
    typed in a note that the input method left as Latin letters."""
    try:
        from .config import CFG
        for k, v in (CFG.get("shorthand") or {}).items():
            text = re.sub(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", v, text, flags=re.I)
    except Exception:
        pass
    return text


def render_line(emoji, text, translation="", glosses=None):
    """Sounds and actions in [square brackets] (or *asterisks*) render as journal annotations
    *[giggles]*, kept outside the quote marks: [crying] Mommy!! → 👧 *[crying]* "Mommy!!" """
    text = _shorthand((text or "").strip())
    # a sound that is the OBJECT of the sentence ("When I hear the [woo] that means recess is over") is part of what she said,
    # not a stage direction: quote it inline instead of pulling it out as an italic annotation.
    def _inline(m):
        word = _sound_text(m)
        before = text[:m.start()].split()
        after = text[m.end():].strip()
        if after and before and len(word.split()) <= 2 and before[-1].lower().strip(",.") in _SOUND_LEADS:
            return f" '{word}' "
        # the owner's own clarification in the middle of a sentence ("please, wear [put on] my pajamas") stays inside the quote
        # as an editorial [bracket], like "his [her] English name" — it is not a sound to pull out of the quote.
        if m.group(0).startswith("[") and before and after and len(word.split()) <= 3 \
                and not re.search(r"[.!?…:;\"”]$", text[:m.start()].rstrip()) and re.match(r"[a-z]", after):
            return " " + m.group(0).replace("[", "\x01").replace("]", "\x02") + " "
        return m.group(0)
    text = re.sub(r"\s{2,}", " ", SOUND.sub(_inline, text)).strip()
    held = text.replace("\x01", "[").replace("\x02", "]") if "\x01" in text else None
    if held is not None and not SOUND.search(text.replace("\x01", "\0").replace("\x02", "\0")):
        return _render_line_core(emoji, held, translation, glosses)
    if not SOUND.search(text):
        return _render_line_core(emoji, text, translation, glosses)
    parts, last = [], 0
    for m in SOUND.finditer(text):
        speech = text[last:m.start()].strip(" ,")
        if speech:
            parts.append(("speech", speech))
        parts.append(("sound", _sound_text(m)))
        last = m.end()
    tail = text[last:].strip(" ,")
    if tail:
        parts.append(("speech", tail))
    out = []
    for kind, val in parts:
        if kind == "sound":
            out.append(f"*[{val}]*")
        else:
            out.append(_render_line_core("", val, translation if kind == "speech" and len(
                [p for p in parts if p[0] == "speech"]) == 1 else "", glosses).strip())
    return (emoji + " " + " ".join(out)).strip()


def _render_line_core(emoji, text, translation="", glosses=None):
    """Build one house-style line. glosses: {汉字run: english} for code-switched words.
    [brackets] inside a translation are yours — implied words like "[Later] I'll feed you" — and stay."""
    translation = (translation or "").strip()
    text = to_simplified(text)
    # a quote inside the quote: This one says "players roar" → This one says 'players roar'
    text = re.sub(r'[“”"]', "'", (text or "").strip())
    glosses = glosses or {}
    if _zh_sentence(text):
        text = re.sub(r"。{2,}|\.{3,}", "……", zh_punct(text))
        line = f'{emoji} "{text}"'.strip()
        if translation:
            line += f" *({translation.strip()})*"
        return line
    # An English (or code-switched) sentence keeps English punctuation, wherever the Chinese keyboard put it ("letters。", "小朋友！")
    text = re.sub(r"[。.]{3,}|…+", "...", text)
    text = re.sub(r"\s*([。，！？：；])\s*", lambda m: _ASCII_P[m.group(1)] + " ", text).strip()
    text = re.sub(r"\s+([.,!?:;])", r"\1", text)
    text = _space_scripts(text)
    body = text
    for run in sorted(glosses, key=len, reverse=True):
        meaning = glosses[run]
        if run in body and meaning:
            body = body.replace(run, f"{run} *({pinyin(run)} / {meaning})*", 1)
    body = re.sub(r"(\)\*)(?=[A-Za-z㐀-鿿])", r"\1 ", body)   # "*(wǒ / I)*pick" → "*(wǒ / I)* pick"
    line = f'{emoji} "{body.strip()}"'.strip()
    if translation and CJK.search(text) and not glosses:   # a code-switched sentence given a whole-line translation instead of chopped glosses
        line += f" *({translation})*"
    return line
