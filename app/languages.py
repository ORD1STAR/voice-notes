"""Languages offered on the start page, and the wording the exports and diagrams use in each.

The code is an ISO 639-1 code, which is what the Groq Whisper `language` parameter expects.
"auto" means the language is not given and Whisper detects it.
"""
import re
from datetime import datetime

AUTO = "auto"

LANGUAGES: dict[str, str] = {
    "auto": "Auto",
    "en": "English",
    "fr": "French",
    "ar": "Arabic",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "tr": "Turkish",
    "zh": "Chinese",
}

# How each language writes its own name.
NATIVE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "Français",
    "ar": "العربية",
    "es": "Español",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "tr": "Türkçe",
    "zh": "中文",
}


def normalize(value: str | None) -> str:
    """Return a supported language code. An empty value means auto; anything unknown raises ValueError."""
    code = (value or "").strip().lower()
    if not code:
        return AUTO
    if code not in LANGUAGES:
        raise ValueError("Unknown language.")
    return code


def lenient(value: str | None) -> str:
    """Like normalize, but an unknown or missing value becomes auto (for notes saved earlier)."""
    try:
        return normalize(value)
    except ValueError:
        return AUTO


def name(code: str | None) -> str:
    return LANGUAGES.get(lenient(code), "Auto")


def allowed_text() -> str:
    return ", ".join(LANGUAGES)


# ------------------------------------------------------------ guessing a language

# Frequent words that tell the Latin script languages apart. Words shared by several languages are left out.
_STOPWORDS: dict[str, set[str]] = {
    "en": set("the and of is that with we this have will are was be you it to for in on they there".split()),
    "fr": set("le les des est une pour qui dans pas donc avec sur mais il faut nous vous du au et ce je sont aussi".split()),
    "es": set("el los las del una para por pero como más muy está también hay se es lo y con".split()),
    "de": set("der die das und ist nicht mit für auf wir ein eine zu den dem sich auch von werden".split()),
    "it": set("il gli della che per non sono ma anche più come è nel delle questo".split()),
    "pt": set("os as do da dos não uma são com mais também você das ao pelo é".split()),
    "tr": set("ve bir bu için ile çok ama daha gibi olarak içinde".split()),
}
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_KANA_HANGUL = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")


def detect(text: str, language: str | None = AUTO) -> str | None:
    """The language of a text: the chosen one, or a guess from its script and common words. None when unsure."""
    code = lenient(language)
    if code != AUTO:
        return code
    letters = sum(1 for c in text if c.isalpha())
    if letters < 12:
        return None
    if len(_ARABIC.findall(text)) / letters > 0.4:
        return "ar"
    if len(_HAN.findall(text)) / letters > 0.3 and not _KANA_HANGUL.search(text):
        return "zh"
    words = re.findall(r"[^\W\d_]+", text.lower())
    scores = sorted(((sum(1 for w in words if w in vocab), lang) for lang, vocab in _STOPWORDS.items()), reverse=True)
    (best, lang), (second, _) = scores[0], scores[1]
    if best >= 3 and best >= second * 1.5:
        return lang
    return None


def notes_text(notes: dict | None) -> str:
    """The words of a notes dict, for guessing its language."""
    notes = notes or {}
    parts = [notes.get("title") or "", notes.get("summary") or ""]
    parts += list(notes.get("key_points") or []) + list(notes.get("actions") or [])
    for sec in notes.get("sections") or []:
        parts.append(sec.get("heading") or "")
        parts += list(sec.get("bullets") or [])
    return " ".join(str(p) for p in parts)


def notes_language(notes: dict | None, language: str | None = AUTO) -> str | None:
    """The language of finished notes: the one chosen for the recording, else a guess. None when unsure."""
    return detect(notes_text(notes), language)


# ------------------------------------------------------------ wording

# Words for the exports (headings, cover, footer) and the diagrams (titles, captions, descriptions).
# English is the fallback for every missing key.
_EN = {
    "summary": "Summary", "key_points": "Key points", "actions": "Action items", "transcript": "Transcript",
    "glance": "At a glance", "language": "Language", "duration": "Duration", "source": "Source file", "page": "Page",
    "mm_title": "Mind map",
    "mm_caption": "The sections of the note and their main points.",
    "mm_caption_cut": "The first {n} of {total} sections and their main points.",
    "mm_alt": "Mind map of {center} with the branches: {branches}",
    "flow_title": "Process",
    "flow_caption": "The steps in the order they were described.",
    "flow_alt": "Process in {n} steps: {steps}",
    "chart_title": "Key figures",
    "chart_caption": "Values mentioned in the note, as {unit}.",
    "chart_alt": "Bar chart of {unit}: {items}",
    "tl_title": "Dates and deadlines",
    "tl_caption": "In the order they were mentioned.",
    "tl_caption_date": "In date order.",
    "tl_alt": "Timeline with {n} dated items: {items}",
    "u_%": "percentages", "u_EUR": "amounts in euros", "u_USD": "amounts in dollars", "u_GBP": "amounts in pounds",
    "u_hours": "hours", "u_minutes": "minutes", "u_days": "days", "u_weeks": "weeks", "u_months": "months",
}

_STRINGS: dict[str, dict[str, str]] = {
    "en": _EN,
    "fr": {
        "summary": "Résumé", "key_points": "Points clés", "actions": "Actions à mener",
        "transcript": "Transcription", "glance": "Vue d'ensemble", "language": "Langue", "duration": "Durée",
        "source": "Fichier source", "page": "Page",
        "mm_title": "Carte mentale",
        "mm_caption": "Les parties de la note et leurs points principaux.",
        "mm_caption_cut": "Les {n} premières parties sur {total} et leurs points principaux.",
        "mm_alt": "Carte mentale de {center} avec les branches : {branches}",
        "flow_title": "Processus",
        "flow_caption": "Les étapes dans l'ordre où elles ont été décrites.",
        "flow_alt": "Processus en {n} étapes : {steps}",
        "chart_title": "Chiffres clés",
        "chart_caption": "Valeurs citées dans la note ({unit}).",
        "chart_alt": "Graphique en barres ({unit}) : {items}",
        "tl_title": "Dates et échéances",
        "tl_caption": "Dans l'ordre où elles ont été citées.",
        "tl_caption_date": "Par ordre chronologique.",
        "tl_alt": "Chronologie de {n} éléments datés : {items}",
        "u_%": "pourcentages", "u_EUR": "montants en euros", "u_USD": "montants en dollars",
        "u_GBP": "montants en livres", "u_hours": "heures", "u_minutes": "minutes", "u_days": "jours",
        "u_weeks": "semaines", "u_months": "mois",
    },
    "es": {
        "summary": "Resumen", "key_points": "Puntos clave", "actions": "Acciones pendientes",
        "transcript": "Transcripción", "glance": "De un vistazo", "language": "Idioma", "duration": "Duración",
        "source": "Archivo de origen", "page": "Página",
        "mm_title": "Mapa mental",
        "mm_caption": "Las secciones de la nota y sus puntos principales.",
        "mm_caption_cut": "Las primeras {n} de {total} secciones y sus puntos principales.",
        "mm_alt": "Mapa mental de {center} con las ramas: {branches}",
        "flow_title": "Proceso",
        "flow_caption": "Los pasos en el orden en que se describieron.",
        "flow_alt": "Proceso en {n} pasos: {steps}",
        "chart_title": "Cifras clave",
        "chart_caption": "Valores mencionados en la nota ({unit}).",
        "chart_alt": "Gráfico de barras ({unit}): {items}",
        "tl_title": "Fechas y plazos",
        "tl_caption": "En el orden en que se mencionaron.",
        "tl_caption_date": "En orden cronológico.",
        "tl_alt": "Cronología de {n} elementos con fecha: {items}",
        "u_%": "porcentajes", "u_EUR": "importes en euros", "u_USD": "importes en dólares",
        "u_GBP": "importes en libras", "u_hours": "horas", "u_minutes": "minutos", "u_days": "días",
        "u_weeks": "semanas", "u_months": "meses",
    },
    "de": {
        "summary": "Zusammenfassung", "key_points": "Kernpunkte", "actions": "Aufgaben", "transcript": "Transkript",
        "glance": "Auf einen Blick", "language": "Sprache", "duration": "Dauer", "source": "Quelldatei", "page": "Seite",
        "mm_title": "Mindmap",
        "mm_caption": "Die Abschnitte der Notiz und ihre Kernpunkte.",
        "mm_caption_cut": "Die ersten {n} von {total} Abschnitten und ihre Kernpunkte.",
        "mm_alt": "Mindmap zu {center} mit den Zweigen: {branches}",
        "flow_title": "Ablauf",
        "flow_caption": "Die Schritte in der Reihenfolge, in der sie beschrieben wurden.",
        "flow_alt": "Ablauf in {n} Schritten: {steps}",
        "chart_title": "Kennzahlen",
        "chart_caption": "In der Notiz genannte Werte ({unit}).",
        "chart_alt": "Balkendiagramm ({unit}): {items}",
        "tl_title": "Termine und Fristen",
        "tl_caption": "In der Reihenfolge, in der sie genannt wurden.",
        "tl_caption_date": "In zeitlicher Reihenfolge.",
        "tl_alt": "Zeitleiste mit {n} Terminen: {items}",
        "u_%": "Prozentwerte", "u_EUR": "Beträge in Euro", "u_USD": "Beträge in Dollar",
        "u_GBP": "Beträge in Pfund", "u_hours": "Stunden", "u_minutes": "Minuten", "u_days": "Tage",
        "u_weeks": "Wochen", "u_months": "Monate",
    },
    "it": {
        "summary": "Riepilogo", "key_points": "Punti chiave", "actions": "Azioni da svolgere",
        "transcript": "Trascrizione", "glance": "In sintesi", "language": "Lingua", "duration": "Durata",
        "source": "File di origine", "page": "Pagina",
        "mm_title": "Mappa mentale",
        "mm_caption": "Le sezioni della nota e i loro punti principali.",
        "mm_caption_cut": "Le prime {n} sezioni su {total} e i loro punti principali.",
        "mm_alt": "Mappa mentale di {center} con i rami: {branches}",
        "flow_title": "Processo",
        "flow_caption": "I passaggi nell'ordine in cui sono stati descritti.",
        "flow_alt": "Processo in {n} passaggi: {steps}",
        "chart_title": "Cifre chiave",
        "chart_caption": "Valori citati nella nota ({unit}).",
        "chart_alt": "Grafico a barre ({unit}): {items}",
        "tl_title": "Date e scadenze",
        "tl_caption": "Nell'ordine in cui sono state citate.",
        "tl_caption_date": "In ordine cronologico.",
        "tl_alt": "Cronologia di {n} elementi con data: {items}",
        "u_%": "percentuali", "u_EUR": "importi in euro", "u_USD": "importi in dollari",
        "u_GBP": "importi in sterline", "u_hours": "ore", "u_minutes": "minuti", "u_days": "giorni",
        "u_weeks": "settimane", "u_months": "mesi",
    },
    "pt": {
        "summary": "Resumo", "key_points": "Pontos principais", "actions": "Tarefas", "transcript": "Transcrição",
        "glance": "Em resumo", "language": "Idioma", "duration": "Duração", "source": "Arquivo de origem",
        "page": "Página",
        "mm_title": "Mapa mental",
        "mm_caption": "As seções da nota e seus pontos principais.",
        "mm_caption_cut": "As primeiras {n} de {total} seções e seus pontos principais.",
        "mm_alt": "Mapa mental de {center} com os ramos: {branches}",
        "flow_title": "Processo",
        "flow_caption": "Os passos na ordem em que foram descritos.",
        "flow_alt": "Processo em {n} passos: {steps}",
        "chart_title": "Números principais",
        "chart_caption": "Valores mencionados na nota ({unit}).",
        "chart_alt": "Gráfico de barras ({unit}): {items}",
        "tl_title": "Datas e prazos",
        "tl_caption": "Na ordem em que foram mencionadas.",
        "tl_caption_date": "Em ordem cronológica.",
        "tl_alt": "Cronologia com {n} itens datados: {items}",
        "u_%": "percentagens", "u_EUR": "valores em euros", "u_USD": "valores em dólares",
        "u_GBP": "valores em libras", "u_hours": "horas", "u_minutes": "minutos", "u_days": "dias",
        "u_weeks": "semanas", "u_months": "meses",
    },
    "tr": {
        "summary": "Özet", "key_points": "Ana noktalar", "actions": "Yapılacaklar", "transcript": "Transkript",
        "glance": "Bir bakışta", "language": "Dil", "duration": "Süre", "source": "Kaynak dosya",
        "page": "Sayfa",
        "mm_title": "Zihin haritası",
        "mm_caption": "Notun bölümleri ve ana noktaları.",
        "mm_caption_cut": "{total} bölümden ilk {n} tanesi ve ana noktaları.",
        "mm_alt": "{center} için zihin haritası, dallar: {branches}",
        "flow_title": "Süreç",
        "flow_caption": "Adımlar anlatıldıkları sırayla.",
        "flow_alt": "{n} adımlı süreç: {steps}",
        "chart_title": "Önemli rakamlar",
        "chart_caption": "Notta geçen değerler ({unit}).",
        "chart_alt": "Çubuk grafik ({unit}): {items}",
        "tl_title": "Tarihler ve son tarihler",
        "tl_caption": "Anıldıkları sırayla.",
        "tl_caption_date": "Tarih sırasına göre.",
        "tl_alt": "{n} tarihli maddeden oluşan zaman çizelgesi: {items}",
        "u_%": "yüzde değerleri", "u_EUR": "euro tutarları", "u_USD": "dolar tutarları",
        "u_GBP": "sterlin tutarları", "u_hours": "saat", "u_minutes": "dakika", "u_days": "gün",
        "u_weeks": "hafta", "u_months": "ay",
    },
    "ar": {
        "summary": "الملخص", "key_points": "النقاط الرئيسية",
        "actions": "المهام", "transcript": "النص الكامل",
        "glance": "نظرة سريعة", "language": "اللغة",
        "duration": "المدة", "source": "الملف المصدر",
        "page": "صفحة",
        "mm_title": "خريطة ذهنية",
        "mm_caption": "أقسام الملاحظة ونقاطها الرئيسية.",
        "mm_caption_cut": "أول {n} من {total} أقسام ونقاطها الرئيسية.",
        "mm_alt": "خريطة ذهنية لـ {center} بالفروع: {branches}",
        "flow_title": "عملية",
        "flow_caption": "الخطوات بالترتيب الذي ذُكرت به.",
        "flow_alt": "عملية من {n} خطوات: {steps}",
        "chart_title": "أرقام رئيسية",
        "chart_caption": "القيم المذكورة في الملاحظة ({unit}).",
        "chart_alt": "مخطط شريطي ({unit}): {items}",
        "tl_title": "التواريخ والمواعيد",
        "tl_caption": "بالترتيب الذي ذُكرت به.",
        "tl_caption_date": "بترتيب التاريخ.",
        "tl_alt": "جدول زمني من {n} عناصر مؤرخة: {items}",
        "u_%": "نسب مئوية", "u_EUR": "مبالغ باليورو",
        "u_USD": "مبالغ بالدولار",
        "u_GBP": "مبالغ بالجنيه الإسترليني",
        "u_hours": "ساعات", "u_minutes": "دقائق",
        "u_days": "أيام", "u_weeks": "أسابيع",
        "u_months": "أشهر",
    },
    "zh": {
        "summary": "摘要", "key_points": "要点", "actions": "待办事项", "transcript": "全文",
        "glance": "概览", "language": "语言", "duration": "时长", "source": "源文件",
        "page": "页",
        "mm_title": "思维导图",
        "mm_caption": "笔记的各个部分及其要点。",
        "mm_caption_cut": "共 {total} 个部分中的前 {n} 个及其要点。",
        "mm_alt": "{center} 的思维导图，分支：{branches}",
        "flow_title": "流程",
        "flow_caption": "按描述顺序排列的步骤。",
        "flow_alt": "{n} 个步骤的流程：{steps}",
        "chart_title": "关键数字",
        "chart_caption": "笔记中提到的数值（{unit}）。",
        "chart_alt": "条形图（{unit}）：{items}",
        "tl_title": "日期与截止时间",
        "tl_caption": "按提到的顺序排列。",
        "tl_caption_date": "按日期顺序排列。",
        "tl_alt": "含 {n} 个带日期事项的时间线：{items}",
        "u_%": "百分比", "u_EUR": "欧元金额", "u_USD": "美元金额",
        "u_GBP": "英镑金额", "u_hours": "小时", "u_minutes": "分钟",
        "u_days": "天", "u_weeks": "周", "u_months": "月",
    },
}


def text(key: str, code: str | None = "en") -> str:
    """A piece of wording in a language; English when the language or the key is missing."""
    table = _STRINGS.get(code or "en") or _EN
    return table.get(key) or _EN[key]


# ------------------------------------------------------------ dates and lengths

_MONTH_NAMES = {
    "en": "January February March April May June July August September October November December",
    "fr": "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    "it": "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "pt": "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro",
    "tr": "Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül Ekim Kasım Aralık",
}


def format_date(when: datetime, code: str | None = "en") -> str:
    """A calendar date written the way the language writes it, for example "27 octobre 2026"."""
    d, y = when.day, when.year
    if code == "ar":
        return when.strftime("%Y-%m-%d")
    if code == "zh":
        return f"{y}年{when.month}月{d}日"
    names = _MONTH_NAMES.get(code or "en") or _MONTH_NAMES["en"]
    month = names.split()[when.month - 1]
    if code in ("es", "pt"):
        return f"{d} de {month} de {y}"
    if code == "de":
        return f"{d}. {month} {y}"
    return f"{d} {month} {y}"


def format_duration(seconds) -> str:
    """A length of time for a cover line, such as "45 s", "12 min" or "1 h 05 min". Empty when unknown."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    if total < 60:
        return f"{total} s"
    minutes = round(total / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"
