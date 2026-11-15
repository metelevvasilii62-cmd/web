#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Прототип новостного агрегатора.
Тема: политика и право.

Что делает скрипт:
  1. Читает список RSS-лент из sources.txt (по одной ленте на строку).
  2. Скачивает каждую ленту один раз (таймаут 15 секунд). Ошибка одной
     ленты логируется и не останавливает остальные.
  3. Парсит RSS 2.0 стандартной библиотекой (xml.etree.ElementTree).
  4. Склеивает свежие новости с историей из data/news.json: убирает дубли
     по ссылке и удаляет новости старше 72 часов.
  5. Собирает index.html из template.html: JSON вместо __NEWS_JSON__,
     дата самой свежей новости в МСК вместо __UPDATED__.

Вежливость к источникам: один запрос на ленту за запуск, понятный
User-Agent (контакт — константа CONTACT_EMAIL ниже).

Авторские права: только заголовок, ссылка, дата, название источника и
короткий анонс из description/summary (<=250 символов, без HTML-тегов).
content:encoded, картинки и полные тексты не используются.

Зависимости: только стандартная библиотека Python 3 (см. requirements.txt).
"""

import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(BASE_DIR, "sources.txt")
DATA_DIR = os.path.join(BASE_DIR, "data")
NEWS_JSON = os.path.join(DATA_DIR, "news.json")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
TEMPLATE_FILE = os.path.join(BASE_DIR, "template.html")

HOURS_LIMIT = 72
FEED_TIMEOUT = 15

# Замените на свою почту — она подставляется в User-Agent при обращении к лентам.
# Слово "контакт" пишем латиницей: HTTP-заголовки должны быть в Latin-1,
# urllib иначе откажется отправлять запрос (кириллицу передать нельзя).
CONTACT_EMAIL = "your@email.com"
USER_AGENT = "NewsAggregator/1.0 (contact: %s)" % CONTACT_EMAIL

try:
    MSK = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:  # Windows без пакета tzdata: фиксированное смещение +3
    MSK = timezone(timedelta(hours=3))


# ---------- helpers ----------

def read_sources(path):
    sources = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                sources.append(line.strip())
    return sources


def fetch_rss(url):
    """Одна вежливая попытка скачать ленту. None — если не удалось."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[!] Не удалось загрузить {url}: {exc}")
        return None


def clean_text(text):
    """Убираем HTML-теги, раскрываем сущности, схлопываем пробелы."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def parse_rss_date(date_str):
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_iso(value):
    """Разбирает ISO-время из JSON; брак/нехватка -> None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def format_msk(dt):
    """ДД.ММ.ГГГГ ЧЧ:ММ в московском времени."""
    try:
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def load_history(path):
    """Прежние новости из data/news.json; если файла нет — пустой список."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] Не удалось прочитать историю {path}: {exc}")
        return []


# ---------- core logic ----------

def parse_feed(xml_text, source_name):
    """Только title, link, pubDate и description/summary — ничего лишнего."""
    items = []
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    items_xml = channel.findall("item") if channel is not None else root.findall(".//item")

    for item in items_xml:
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        desc_el = item.find("description")
        if desc_el is None:
            desc_el = item.find("summary")

        title = clean_text(title_el.text) if title_el is not None and title_el.text else ""
        link = clean_text(link_el.text) if link_el is not None and link_el.text else ""
        pub_date_str = clean_text(pub_el.text) if pub_el is not None and pub_el.text else ""
        description = clean_text(desc_el.text) if desc_el is not None and desc_el.text else ""

        if not title or not link:
            continue

        pub_date = parse_rss_date(pub_date_str)
        excerpt = description if description else title
        if len(excerpt) > 250:
            excerpt = excerpt[:250].rsplit(" ", 1)[0] + "…"

        items.append({
            "title": title,
            "source": source_name,
            "link": link,
            "time": pub_date.isoformat() if pub_date else pub_date_str,
            "pub_date": pub_date,
            "excerpt": excerpt,
        })

    return items


def main():
    sources = read_sources(SOURCES_FILE)
    print(f"[OK] Источников в списке: {len(sources)}")

    seen_links = set()
    fresh = []
    loaded_feeds = 0

    for url in sources:
        source_name = urlparse(url).netloc.replace("www.", "")
        xml_text = fetch_rss(url)
        if xml_text is None:
            continue
        loaded_feeds += 1
        items = parse_feed(xml_text, source_name)
        print(f"    {source_name}: лента загружена, записей -> {len(items)}")
        for item in items:
            key = (item["link"] or "").strip()
            if not key or key in seen_links:
                continue
            if item["pub_date"] is None:
                continue
            if datetime.now(timezone.utc) - item["pub_date"] > timedelta(hours=HOURS_LIMIT):
                continue
            seen_links.add(key)
            fresh.append(item)

    if loaded_feeds == 0:
        print("[!] Не загрузилась ни одна лента — index.html не перезаписываем.")
        sys.exit(1)

    # История + свежие; дубли по ссылке, свежие данные важнее истории.
    merged = {}
    for n in load_history(NEWS_JSON):
        link = (n.get("link") or "").strip()
        if link:
            merged[link] = n
    for n in fresh:
        merged[n["link"]] = n

    # Удаляем новости старше HOURS_LIMIT часов (отсчёт от текущего момента в UTC).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LIMIT)
    news = []
    for n in merged.values():
        dt = parse_iso(n.get("time"))
        if dt is not None and dt < cutoff:
            continue
        news.append(n)

    news.sort(
        key=lambda n: parse_iso(n.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    output = [
        {
            "title": n["title"],
            "source": n["source"],
            "time": n["time"],
            "link": n["link"],
            "excerpt": n["excerpt"],
        }
        for n in news
    ]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NEWS_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    updated = format_msk(parse_iso(output[0]["time"])) if output else "—"
    generate_html(output, updated)

    print(f"[OK] Лент загружено: {loaded_feeds} из {len(sources)}")
    print(f"[OK] Новостей (последние {HOURS_LIMIT} ч): {len(output)}")
    print(f"[OK] Обновлено: {updated} (МСК)")
    print(f"[OK] Сохранено: {NEWS_JSON}")
    print(f"[OK] HTML: {INDEX_HTML}")


def generate_html(news, updated):
    """Собирает index.html из template.html: JSON и дата обновления (МСК)."""
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        template = f.read()

    payload = json.dumps(news, ensure_ascii=False, indent=2)
    # Экранируем «</», чтобы JSON не закрывал тег <script> в template.html.
    payload = payload.replace("</", "<\\/")

    html_doc = template.replace("__UPDATED__", updated).replace("__NEWS_JSON__", payload)

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html_doc)


if __name__ == "__main__":
    main()
