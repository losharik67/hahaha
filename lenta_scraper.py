"""Сбор корпуса новостей с Lenta.ru через sitemap и BeautifulSoup.

Установка: pip install requests beautifulsoup4 lxml
Тест: python lenta_scraper.py --limit 10
Сбор: python lenta_scraper.py --limit 4000
"""

import argparse
import csv
import gzip
import json
import random
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://lenta.ru"
DEFAULT_SITEMAP = "https://lenta.ru/sitemap.xml.gz"
USER_AGENT = "AcademicNewsCorpusBot/1.0 (noncommercial research)"
FIELDS = ("url", "title", "date", "category", "text")
ARTICLE_PATTERN = r"^/(?:news|articles)/\d{4}/\d{2}/\d{2}/[^/]+/?$"


class BlockedError(Exception):
    pass


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def normalized_url(url):
    """Удаляем якорь и трекинговые параметры, но не путь."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, "", ""))


class PoliteClient:
    def __init__(self, base, delay, jitter):
        self.base = base.rstrip("/")
        self.domain = urlsplit(base).hostname
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept": "text/html,application/xml,text/xml,*/*"})
        self.delay = delay
        self.jitter = jitter
        self.last_request = None
        self.robots = RobotFileParser()
        self.robots.set_url(self.base + "/robots.txt")
        self.sitemap_urls = []

    def _pause(self):
        if self.last_request is not None:
            elapsed = time.monotonic() - self.last_request
            remaining = self.delay + random.uniform(0, self.jitter) - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self.last_request = time.monotonic()

    def _request(self, url):
        for attempt in range(3):
            self._pause()
            try:
                result = self.session.get(url, timeout=(10, 30))
            except requests.RequestException as exc:
                print(f"[Сеть] {url}: {exc}", file=sys.stderr)
                if attempt == 2:
                    return None
                time.sleep(5 * (attempt + 1))
                continue

            if result.status_code in (403, 429):
                retry_after = result.headers.get("Retry-After", "не указан")
                raise BlockedError(
                    f"Сервер ответил {result.status_code} для {url}; "
                    f"Retry-After: {retry_after}. Сбор прекращён."
                )
            if result.status_code in (500, 502, 503, 504):
                print(f"[HTTP {result.status_code}] {url}", file=sys.stderr)
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                return None
            if result.status_code == 404 and url.endswith("/robots.txt"):
                return result  # отсутствие robots.txt не является запретом обхода
            if not result.ok:
                print(f"[HTTP {result.status_code}] {url}", file=sys.stderr)
                return None
            if urlsplit(result.url).hostname not in (self.domain, f"www.{self.domain}"):
                print(f"[Пропуск] Переадресация на другой сайт: {result.url}")
                return None
            return result
        return None

    def load_robots(self):
        response = self._request(self.base + "/robots.txt")
        if response is None:
            raise RuntimeError("Не удалось прочитать robots.txt. Сбор не начат.")
        lines = response.text.splitlines() if response.status_code == 200 else []
        self.robots.parse(lines)
        self.sitemap_urls = [
            clean(line.split(":", 1)[1])
            for line in lines
            if line.lower().lstrip().startswith("sitemap:")
        ]
        crawl_delay = self.robots.crawl_delay(USER_AGENT)
        if crawl_delay:
            self.delay = max(self.delay, float(crawl_delay))
        print(f"robots.txt проверен. Минимальная пауза: {self.delay:g} с (+ случайная).")

    def get(self, url):
        if urlsplit(url).hostname not in (self.domain, f"www.{self.domain}"):
            return None
        if not self.robots.can_fetch(USER_AGENT, url):
            print(f"[robots.txt] Пропуск: {url}")
            return None
        return self._request(url)


def parse_sitemap(data):
    """Возвращает (тип карты, [(url, lastmod), ...])."""
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    soup = BeautifulSoup(data, "xml")
    if soup.find("sitemapindex"):
        entries = soup.select("sitemapindex > sitemap")
        kind = "index"
    elif soup.find("urlset"):
        entries = soup.select("urlset > url")
        kind = "urls"
    else:
        raise ValueError("Не распознан XML sitemap")
    items = []
    for item in entries:
        loc = item.find("loc", recursive=False)
        lastmod = item.find("lastmod", recursive=False)
        if loc and clean(loc.get_text()):
            items.append((clean(loc.get_text()), clean(lastmod.get_text()) if lastmod else ""))
    items.sort(key=lambda x: x[1], reverse=True)
    return kind, items


def article_urls(client, sitemap, pattern):
    pending = deque([sitemap])
    seen_maps = set()
    seen_articles = set()
    while pending:
        map_url = pending.popleft()
        if map_url in seen_maps:
            continue
        seen_maps.add(map_url)
        response = client.get(map_url)
        if response is None:
            continue
        try:
            kind, items = parse_sitemap(response.content)
        except (ValueError, OSError, EOFError) as exc:
            print(f"[Sitemap] Не удалось разобрать {map_url}: {exc}")
            continue
        if kind == "index":
            for url, _ in items:
                if url not in seen_maps:
                    pending.append(url)
            print(f"Карта-индекс: найдено {len(items)} вложенных карт")
            continue
        print(f"Карта URL: {len(items)} адресов ({map_url})")
        for url, _ in items:
            url = normalized_url(url)
            if url in seen_articles:
                continue
            parsed = urlsplit(url)
            if parsed.hostname not in (client.domain, f"www.{client.domain}"):
                continue
            if not pattern.search(parsed.path):
                continue
            seen_articles.add(url)
            yield url


def jsonld_objects(soup):
    """Извлекает объекты JSON-LD, включая вложенные @graph."""
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, ValueError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            item = queue.pop()
            if isinstance(item, dict):
                yield item
                graph = item.get("@graph", [])
                queue.extend(graph if isinstance(graph, list) else [graph])


def paragraphs_text(elements):
    pieces = []
    seen = set()
    for element in elements:
        for p in ([element] if element.name == "p" else element.find_all("p")):
            # Исключаем ссылки на рекомендации, рекламу и содержимое виджетов.
            if p.find_parent(["aside", "nav", "footer", "figure"]):
                continue
            value = clean(p.get_text(" ", strip=True))
            if len(value) >= 20 and value not in seen:
                pieces.append(value)
                seen.add(value)
    return "\n\n".join(pieces)


def parse_article(html, url, min_chars=150):
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.select_one("h1[itemprop='headline'], h1.topic-body__title, article h1, h1")
    og_title = soup.select_one('meta[property="og:title"]')
    title = clean(title_tag.get_text(" ", strip=True)) if title_tag else ""
    if not title and og_title:
        title = clean(og_title.get("content"))

    date_tag = soup.select_one('meta[property="article:published_time"], meta[itemprop="datePublished"]')
    time_tag = soup.select_one("time[datetime]")
    published = (date_tag.get("content") if date_tag else None) or (
        time_tag.get("datetime") if time_tag else "")
    section_tag = soup.select_one('meta[property="article:section"]')
    category = clean(section_tag.get("content")) if section_tag else ""

    # У Lenta.ru текст часто находится в <p class="topic-body__content-text">.
    text = paragraphs_text(soup.select("p.topic-body__content-text"))
    if len(text) < min_chars:
        for selector in (
            '[itemprop="articleBody"]',
            ".topic-body__content", ".article__text", ".b-text", "article"
        ):
            for container in soup.select(selector):
                candidate = paragraphs_text([container])
                if len(candidate) > len(text):
                    text = candidate
            if len(text) >= min_chars:
                break

    # Для газет с другой HTML-вёрсткой используем структурированные данные.
    for item in jsonld_objects(soup):
        if not published and isinstance(item.get("datePublished"), str):
            published = item["datePublished"]
        if not category and isinstance(item.get("articleSection"), str):
            category = item["articleSection"]
        if not title and isinstance(item.get("headline"), str):
            title = clean(item["headline"])
        if len(text) < min_chars and isinstance(item.get("articleBody"), str):
            candidate = clean(item["articleBody"])
            if len(candidate) > len(text):
                text = candidate

    if not title or len(text) < min_chars:
        return None
    return {"url": url, "title": title, "date": published,
            "category": category, "text": text}


def existing_urls(path):
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return {row["url"] for row in csv.DictReader(f) if row.get("url")}


def main():
    ap = argparse.ArgumentParser(description="Сбор новостных статей через sitemap + BeautifulSoup")
    ap.add_argument("--limit", type=int, default=4000, help="Нужное число статей (всего в файле)")
    ap.add_argument("--output", default="lenta_4000.csv", help="CSV-файл результата")
    ap.add_argument("--base", default=BASE, help="Сайт, например https://lenta.ru")
    ap.add_argument("--sitemap", default=None, help="URL карты сайта (если не указан, из robots.txt)")
    ap.add_argument("--pattern", default=ARTICLE_PATTERN, help="Regex для путей статей")
    ap.add_argument("--delay", type=float, default=2.5, help="Минимум секунд между запросами")
    ap.add_argument("--jitter", type=float, default=1.5, help="Дополнительная случайная пауза")
    ap.add_argument("--min-chars", type=int, default=150, help="Минимальная длина текста")
    args = ap.parse_args()
    if args.limit < 1 or args.delay < 0 or args.jitter < 0:
        ap.error("--limit должно быть >= 1; паузы должны быть >= 0")

    output = Path(args.output)
    done = existing_urls(output)
    print(f"Уже сохранено: {len(done)} статей. Цель: {args.limit}.")
    if len(done) >= args.limit:
        print("Нужное число статей уже собрано.")
        return

    client = PoliteClient(args.base, args.delay, args.jitter)
    try:
        client.load_robots()
        sitemaps = ([args.sitemap] if args.sitemap else
                    client.sitemap_urls or [client.base + "/sitemap.xml.gz"])
        output.parent.mkdir(parents=True, exist_ok=True)
        is_new = not output.exists() or output.stat().st_size == 0
        with output.open("a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new:
                writer.writeheader()
            tried = 0
            for sitemap in sitemaps:
                for url in article_urls(client, sitemap, re.compile(args.pattern)):
                    if url in done:
                        continue
                    tried += 1
                    response = client.get(url)
                    if response is None:
                        continue
                    article = parse_article(response.content, url, args.min_chars)
                    if article is None:
                        if tried % 20 == 0:
                            print(f"Просмотрено {tried} новых адресов; статей: {len(done)}")
                        continue
                    writer.writerow(article)
                    f.flush()  # данные не теряются при остановке
                    done.add(url)
                    print(f"[{len(done)}/{args.limit}] {article['title'][:80]}")
                    if len(done) >= args.limit:
                        print(f"Готово: {len(done)} статей -> {output.resolve()}")
                        return
        print(f"Карты сайта исчерпаны. Сохранено {len(done)} статей -> {output.resolve()}")
    except KeyboardInterrupt:
        print(f"\nОстановлено вручную. Уже сохранено: {len(done)}. Запустите команду ещё раз.")
    except BlockedError as exc:
        print(f"\n{exc}\nСохранено: {len(done)}. Не обходите блокировку.", file=sys.stderr)
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
