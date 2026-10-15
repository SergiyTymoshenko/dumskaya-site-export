#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка сайта с Weblium (по умолчанию https://dumskaya-design.com) для переезда
на свой сервер.

Что делает скрипт:
  1. Собирает ВСЕ адреса страниц всех языков: sitemap.xml + обход ссылок +
     связки языков (hreflang). Для каждого адреса записывает код ответа,
     редиректы, title, description, h1, canonical.
  2. Сохраняет исходный HTML каждой страницы (ru / ua / en) как резервную копию.
  3. Скачивает все картинки в ОРИГИНАЛЬНОМ качестве (файл, загруженный в Weblium).
     Если оригинал недоступен, берёт самую большую доступную версию.
  4. Для русских страниц собирает content.md: тексты и картинки в порядке страницы.
  5. Пишет таблицы urls.csv, url_map.csv, images.csv и отчёт report.txt.

Повторный запуск безопасен: уже скачанные картинки пропускаются,
докачивается только недостающее.

Запуск:  python3 weblium_export.py --out ~/dumskaya-export
"""

import argparse
import csv
import html as htmlmod
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

try:
    from PIL import Image as PILImage
except ImportError:  # размеры картинок просто не попадут в таблицу
    PILImage = None

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

LANGS = ("ru", "ua", "en")
HREFLANG = {"ru": "ru", "uk": "ua", "ua": "ua", "en": "en"}

# Картинки Weblium: //res2.weblium.site/res/<id сайта>/<id файла>[_optimized[_1316]][.webp]
# Без суффикса отдаётся оригинал, загруженный в редактор.
WEBLIUM_RE = re.compile(
    r"(?:(?:https?:)?//)?(res\d*\.weblium\.site)/res/([A-Za-z0-9_-]+)/([0-9a-f]{24})([A-Za-z0-9_.\-]*)")
# Картинки магазина Weblium: //e-c.storage.googleapis.com/res/<uuid>/<original|480|112>
ECOM_RE = re.compile(
    r"(?:(?:https?:)?//)?(e-c\.storage\.googleapis\.com)/res/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(/[A-Za-z0-9_.\-]*)?")

SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|pdf|zip|rar|mp4|webm|mov|ico|css|js|xml|txt|json|docx?|xlsx?)$", re.I)

CT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/svg+xml": ".svg", "image/avif": ".avif", "image/heic": ".heic",
    "image/tiff": ".tif", "image/bmp": ".bmp", "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "header", "hr", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "thead", "tfoot", "tr", "ul", "picture", "label",
}
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "template", "iframe", "head", "meta", "link",
    "input", "select", "textarea", "option", "canvas", "video", "audio", "object",
}
# <source> намеренно не пропускаем: парсер lxml не считает его пустым тегом
# и «вкладывает» в него следующий <img> внутри <picture>.
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def lang_of(path):
    first = path.strip("/").split("/", 1)[0]
    return first if first in ("en", "ua") else "ru"


def strip_lang(path):
    lang = lang_of(path)
    if lang == "ru":
        return path
    rest = path[len("/" + lang):]
    return rest or "/"


def rel_page_dir(path):
    """/en/foo/bar -> pages/en/foo/bar ; / -> pages/ru/_home"""
    lang = lang_of(path)
    parts = [p for p in path.strip("/").split("/") if p]
    if lang != "ru":
        parts = parts[1:]
    safe = [re.sub(r'[\\:*?"<>|\x00-\x1f]', "_", unquote(p)) for p in parts]
    safe = [p if p not in (".", "..") else "_" for p in safe]
    return os.path.join("pages", lang, *(safe or ["_home"]))


def attr_blob(tag):
    vals = []
    for v in tag.attrs.values():
        vals.append(v if isinstance(v, str) else " ".join(map(str, v)))
    return " ".join(vals)


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{n} {unit}"
        n /= 1024.0
    return str(n)


class Exporter:
    def __init__(self, args):
        self.base = args.site.rstrip("/")
        self.host = urlparse(self.base).netloc.lower()
        self.out = os.path.abspath(os.path.expanduser(args.out))
        self.page_workers = max(1, args.page_workers)
        self.image_workers = max(1, args.image_workers)
        self.content_langs = {x.strip() for x in args.content_langs.split(",") if x.strip()}
        self.max_pages = args.max_pages
        self.skip_images = args.skip_images
        self.cdn_override = []
        for item in args.cdn_override:
            src, _, dst = item.partition("=")
            self.cdn_override.append((src.rstrip("/"), dst.rstrip("/")))
        self.lock = threading.Lock()
        self.local = threading.local()
        self.pages = {}    # path -> запись о странице
        self.images = {}   # ключ -> запись о картинке
        self.errors = []
        os.makedirs(self.out, exist_ok=True)
        self.log_fh = open(os.path.join(self.out, "export.log"), "a", encoding="utf-8")

    # ---------- служебное ----------
    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with self.lock:
            self.log_fh.write(line + "\n")
            self.log_fh.flush()

    def err(self, msg):
        with self.lock:
            self.errors.append(msg)
        self.log("ОШИБКА: " + msg)

    def sess(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "ru,uk;q=0.9,en;q=0.8"})
            self.local.s = s
        return s

    def get(self, url, stream=False):
        delay, last = 2, None
        for _ in range(5):
            try:
                r = self.sess().get(url, timeout=(20, 180), stream=stream, allow_redirects=True)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                last = f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After", "")
                r.close()
                if ra.isdigit():
                    delay = max(delay, min(int(ra), 120))
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise RuntimeError(last or "неизвестная ошибка")

    def cdn(self, url):
        for src, dst in self.cdn_override:  # только для тестов
            if url.startswith(src):
                return dst + url[len(src):]
        return url

    def norm_path(self, href, base_url):
        if not href:
            return None
        href = href.strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "viber:", "tg:", "whatsapp:")):
            return None
        u = urlparse(urljoin(base_url, href))
        if u.scheme not in ("http", "https"):
            return None
        host = u.netloc.lower().removeprefix("www.")
        if host != self.host.removeprefix("www."):
            return None
        path = re.sub(r"/{2,}", "/", u.path or "/")
        if len(path) > 1:
            path = path.rstrip("/")
        if SKIP_EXT.search(path):
            return None
        return path or "/"

    def url_of(self, path):
        return self.base + path if path != "/" else self.base + "/"

    # ---------- картинки: поиск ----------
    def keys_in(self, text):
        keys = []
        if not text:
            return keys
        text = htmlmod.unescape(text.replace("\\u002F", "/").replace("\\u002f", "/").replace("\\/", "/"))
        for m in WEBLIUM_RE.finditer(text):
            keys.append(("w", m.group(1), m.group(2), m.group(3), m.group(4) or ""))
        for m in ECOM_RE.finditer(text):
            keys.append(("e", m.group(1), "", m.group(2), m.group(3) or ""))
        return keys

    def register_images(self, raw_text, soup, path, lang):
        found = set()
        for kind, host, site, iid, suffix in self.keys_in(raw_text):
            key = iid
            if kind == "w":
                variant = f"https://{host}/res/{site}/{iid}{suffix}"
            else:
                variant = f"https://{host}/res/{iid}{suffix}"
            with self.lock:
                rec = self.images.get(key)
                if rec is None:
                    rec = self.images[key] = {
                        "id": iid, "kind": kind, "host": host, "site": site,
                        "variants": set(), "pages": set(), "alt": {},
                    }
                if variant != (f"https://{host}/res/{site}/{iid}" if kind == "w" else f"https://{host}/res/{iid}"):
                    rec["variants"].add(variant)
                rec["pages"].add(path)
            found.add(key)
        # alt-тексты (пригодятся для SEO на новом сайте)
        for img in soup.find_all("img"):
            alt = clean(img.get("alt") or img.get("aria-label") or "")
            if not alt:
                continue
            for k in self.keys_in(attr_blob(img)):
                with self.lock:
                    rec = self.images.get(k[3])
                    if rec is not None:
                        rec["alt"].setdefault(lang, alt)
        return found

    # ---------- этап 1: страницы ----------
    def sitemap_paths(self):
        paths, queue, seen = [], [self.base + "/sitemap.xml"], set()
        site_dir = os.path.join(self.out, "site")
        os.makedirs(os.path.join(site_dir, "sitemaps"), exist_ok=True)
        try:
            r = self.get(self.base + "/robots.txt")
            if r.status_code == 200:
                with open(os.path.join(site_dir, "robots.txt"), "wb") as f:
                    f.write(r.content)
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        queue.append(line.split(":", 1)[1].strip())
        except Exception as e:  # noqa: BLE001
            self.err(f"robots.txt: {e}")
        while queue:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                r = self.get(sm)
            except Exception as e:  # noqa: BLE001
                self.err(f"sitemap {sm}: {e}")
                continue
            if r.status_code != 200:
                continue
            name = urlparse(sm).path.strip("/").replace("/", "__") or "sitemap.xml"
            with open(os.path.join(site_dir, "sitemaps", name), "wb") as f:
                f.write(r.content)
            locs = [htmlmod.unescape(x) for x in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)]
            if "<sitemapindex" in r.text:
                queue.extend(locs)
            else:
                for loc in locs:
                    p = self.norm_path(loc, self.base)
                    if p:
                        paths.append(p)
        self.log(f"В sitemap найдено адресов страниц: {len(paths)}")
        return paths

    def process_page(self, path):
        try:
            return self._process_page(path)
        except Exception as e:  # noqa: BLE001 — одна сломанная страница не должна останавливать выгрузку
            self.err(f"страница {path}: {type(e).__name__}: {e}")
            return {"path": path, "lang": lang_of(path), "url": self.url_of(path),
                    "alternates": {}, "status": "ERROR"}, []

    def _process_page(self, path):
        url = self.url_of(path)
        rec = {"path": path, "lang": lang_of(path), "url": url, "alternates": {}}
        try:
            r = self.get(url)
        except Exception as e:  # noqa: BLE001
            rec["status"] = "ERROR"
            self.err(f"страница {url}: {e}")
            return rec, []
        rec["status"] = r.status_code
        if r.history:
            # Редирект на старом сайте: запоминаем, куда ведёт, а саму целевую страницу
            # обрабатываем отдельно по её собственному адресу.
            rec["status"] = r.history[0].status_code
            rec["redirects"] = " > ".join(str(h.status_code) for h in r.history) + f" > {r.status_code}"
            rec["final_url"] = r.url
            target = self.norm_path(r.url, r.url)
            return rec, [target] if target else []
        if "html" not in (r.headers.get("content-type") or ""):
            rec["note"] = "не HTML"
            return rec, []
        page_dir = os.path.join(self.out, rel_page_dir(path))
        os.makedirs(page_dir, exist_ok=True)
        with open(os.path.join(page_dir, "page.html"), "wb") as f:
            f.write(r.content)
        rec["html_file"] = os.path.relpath(os.path.join(page_dir, "page.html"), self.out)

        text = r.text
        soup = BeautifulSoup(text, PARSER)
        title = soup.find("title")
        rec["title"] = clean(title.get_text()) if title else ""
        md = soup.find("meta", attrs={"name": "description"})
        rec["description"] = clean(md.get("content")) if md else ""
        h1 = soup.find("h1")
        rec["h1"] = clean(h1.get_text(" ")) if h1 else ""
        can = soup.find("link", rel="canonical")
        rec["canonical"] = can.get("href", "") if can else ""
        rob = soup.find("meta", attrs={"name": "robots"})
        rec["robots"] = rob.get("content", "") if rob else ""
        og = soup.find("meta", attrs={"property": "og:image"})
        rec["og_image"] = og.get("content", "") if og else ""

        links = set()
        for ln in soup.find_all("link", hreflang=True):
            lang = HREFLANG.get(ln.get("hreflang", "").lower().split("-")[0])
            p = self.norm_path(ln.get("href"), r.url)
            if lang and p:
                rec["alternates"][lang] = p
                links.add(p)
        if r.status_code == 200:
            for a in soup.find_all("a", href=True):
                p = self.norm_path(a["href"], r.url)
                if p:
                    links.add(p)
        rec["image_keys"] = self.register_images(text, soup, path, rec["lang"])
        return rec, sorted(links)

    def crawl(self):
        start = ["/", "/ua", "/en"] + self.sitemap_paths()
        seen, queue = set(), []
        for p in start:
            if p not in seen:
                seen.add(p)
                queue.append(p)
        done = 0
        with ThreadPoolExecutor(self.page_workers) as ex:
            while queue:
                batch, queue = queue, []
                for rec, links in ex.map(self.process_page, batch):
                    self.pages[rec["path"]] = rec
                    done += 1
                    if done % 25 == 0:
                        self.log(f"Страниц обработано: {done}, картинок найдено: {len(self.images)}")
                    for p in links:
                        if p not in seen:
                            if len(seen) >= self.max_pages:
                                self.err(f"достигнут лимит --max-pages={self.max_pages}, адрес пропущен: {p}")
                                continue
                            seen.add(p)
                            queue.append(p)
        self.log(f"Страниц всего: {len(self.pages)}, уникальных картинок: {len(self.images)}")

    # ---------- этап 2: картинки ----------
    def image_dir(self, rec):
        return "images" if rec["kind"] == "w" else os.path.join("images", "shop")

    def candidates(self, rec):
        if rec["kind"] == "w":
            base = f"https://{rec['host']}/res/{rec['site']}/{rec['id']}"
            first = [base, base + "_optimized"]

            def width(u):
                m = re.search(r"_optimized_(\d+)", u)
                return int(m.group(1)) if m else 0
        else:
            base = f"https://{rec['host']}/res/{rec['id']}"
            first = [base + "/original"]

            def width(u):
                m = re.search(r"/(\d+)$", u)
                return int(m.group(1)) if m else 0
        rest = sorted(rec["variants"], key=lambda u: (-width(u), u.endswith(".webp"), u))
        out = []
        for u in first + rest:
            if u not in out:
                out.append(u)
        return out

    def fill_dims(self, rec):
        if PILImage is None or not rec.get("file"):
            return
        try:
            with PILImage.open(os.path.join(self.out, rec["file"])) as im:
                rec["width"], rec["height"] = im.size
        except Exception:  # noqa: BLE001 — не картинка (видео/pdf) или формат без поддержки
            pass

    def download(self, rec):
        sub = self.image_dir(rec)
        d = os.path.join(self.out, sub)
        have = self.existing.get((sub, rec["id"]))
        if have:
            path = os.path.join(d, have)
            rec.update(file=os.path.join(sub, have), bytes=os.path.getsize(path),
                       state="уже было", quality=rec.get("quality") or "оригинал")
            self.fill_dims(rec)
            return rec
        last = "нет вариантов"
        for i, url in enumerate(self.candidates(rec)):
            real = self.cdn(url)
            try:
                r = self.get(real, stream=True)
            except Exception as e:  # noqa: BLE001
                last = str(e)
                continue
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                r.close()
                continue
            ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ct.startswith(("image/", "video/", "application/pdf", "application/octet-stream")):
                last = f"неожиданный тип {ct or '?'}"
                r.close()
                continue
            ext = CT_EXT.get(ct) or os.path.splitext(urlparse(url).path)[1].lower() or ".bin"
            name = rec["id"] + ext
            tmp = os.path.join(d, name + ".part")
            n = 0
            try:
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
                            n += len(chunk)
            except Exception as e:  # noqa: BLE001
                last = f"обрыв загрузки: {e}"
                if os.path.exists(tmp):
                    os.remove(tmp)
                continue
            finally:
                r.close()
            cl = r.headers.get("content-length")
            if n == 0 or (cl and cl.isdigit() and not r.headers.get("content-encoding") and int(cl) != n):
                last = f"файл недокачан ({n} из {cl})"
                os.remove(tmp)
                continue
            os.replace(tmp, os.path.join(d, name))
            rec.update(file=os.path.join(sub, name), bytes=n, content_type=ct, source_url=url,
                       state="скачано", quality="оригинал" if i == 0 else "запасная версия")
            self.fill_dims(rec)
            return rec
        rec.update(state="НЕ СКАЧАНО", error=last)
        self.err(f"картинка {rec['id']}: {last}")
        return rec

    def download_images(self):
        self.existing = {}
        for sub in ("images", os.path.join("images", "shop")):
            d = os.path.join(self.out, sub)
            os.makedirs(d, exist_ok=True)
            for fn in os.listdir(d):
                full = os.path.join(d, fn)
                if fn.startswith(".") or not os.path.isfile(full):
                    continue
                if fn.endswith(".part"):
                    os.remove(full)
                    continue
                if os.path.getsize(full) > 0:
                    self.existing[(sub, fn.rsplit(".", 1)[0])] = fn
        index_path = os.path.join(self.out, "images", ".index.json")
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, ValueError):
            index = {}
        recs = list(self.images.values())
        for rec in recs:  # сведения о файлах из прошлого запуска
            old = index.get(rec["id"])
            if old and self.existing.get((self.image_dir(rec), rec["id"])):
                for k in ("quality", "source_url", "content_type"):
                    if old.get(k):
                        rec[k] = old[k]
        self.log(f"Картинок к загрузке: {len(recs)} (уже скачано ранее: {len(self.existing)})")
        done, total_bytes = 0, 0
        with ThreadPoolExecutor(self.image_workers) as ex:
            for rec in ex.map(self.download, recs):
                done += 1
                total_bytes += rec.get("bytes", 0)
                if rec.get("file"):
                    index[rec["id"]] = {k: rec.get(k, "") for k in ("file", "quality", "source_url", "content_type")}
                if done % 50 == 0 or done == len(recs):
                    self.log(f"Картинок: {done}/{len(recs)}, объём {human(total_bytes)}")
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=0)

    # ---------- этап 3: тексты ----------
    def image_ref(self, key, page_dir):
        rec = self.images.get(key)
        if rec is None:
            return None
        if rec.get("file"):
            return os.path.relpath(os.path.join(self.out, rec["file"]), page_dir).replace(os.sep, "/")
        return self.candidates(rec)[0]

    def link_target(self, href, page_url):
        if not href:
            return None
        href = href.strip()
        if href.startswith(("tel:", "mailto:")):
            return href
        if href.startswith(("#", "javascript:")):
            return None
        p = self.norm_path(href, page_url)
        if p:
            return p
        full = urljoin(page_url, href)
        return full if urlparse(full).scheme in ("http", "https") else None

    def markdown_blocks(self, soup, rec, page_dir):
        blocks = [b for b in soup.select("div.block-wrapper") if not b.find_parent("div", class_="block-wrapper")]
        if not blocks:
            root = soup.select_one("#app") or soup.body or soup
            blocks = [root]
        parts = []
        for b in blocks:
            if b.select_one('[data-block-content-type="header"]') or b.select_one(".w-section--footer"):
                continue
            lines = MD(self, rec["url"], page_dir).convert(b)
            if lines:
                bid = b.get("id") or "block"
                parts.append(f"<!-- {bid} -->")
                parts.extend(lines)
        return parts

    def build_content(self):
        made = 0
        for path in sorted(self.pages):
            rec = self.pages[path]
            if rec["lang"] not in self.content_langs or rec.get("status") != 200 or not rec.get("html_file"):
                continue
            html_path = os.path.join(self.out, rec["html_file"])
            page_dir = os.path.dirname(html_path)
            with open(html_path, "rb") as f:
                soup = BeautifulSoup(f.read().decode("utf-8", "replace"), PARSER)
            fm = ["---", f"url: {rec['url']}"]
            for k in ("title", "description", "h1"):
                fm.append(f"{k}: {json.dumps(rec.get(k, ''), ensure_ascii=False)}")
            for lang in LANGS:
                if lang != rec["lang"] and rec["alternates"].get(lang):
                    fm.append(f"{lang}: {rec['alternates'][lang]}")
            fm.append("---")
            body = self.markdown_blocks(soup, rec, page_dir)
            with open(os.path.join(page_dir, "content.md"), "w", encoding="utf-8") as f:
                f.write("\n".join(fm) + "\n\n" + "\n\n".join(body) + "\n")
            made += 1
        self.log(f"Собрано content.md: {made}")
        self.build_site_parts()

    def build_site_parts(self):
        """Меню и подвал сайта — один раз, с главной русской страницы."""
        home = self.pages.get("/")
        if not home or not home.get("html_file"):
            return
        site_dir = os.path.join(self.out, "site")
        os.makedirs(site_dir, exist_ok=True)
        html_path = os.path.join(self.out, home["html_file"])
        with open(html_path, "rb") as f:
            soup = BeautifulSoup(f.read().decode("utf-8", "replace"), PARSER)
        header = soup.select_one('[data-block-content-type="header"]')
        if header:
            seen, lines = set(), ["# Меню сайта (с главной страницы)", ""]
            for a in header.find_all("a", href=True):
                t, link = clean(a.get_text(" ")), self.link_target(a["href"], home["url"])
                if t and link and (t, link) not in seen:
                    seen.add((t, link))
                    lines.append(f"- [{t}]({link})")
            with open(os.path.join(site_dir, "menu.md"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        footer = soup.select_one(".w-section--footer")
        if footer:
            lines = MD(self, home["url"], site_dir).convert(footer)
            with open(os.path.join(site_dir, "footer.md"), "w", encoding="utf-8") as f:
                f.write("# Подвал сайта\n\n" + "\n\n".join(lines) + "\n")
        icon = soup.find("link", rel=lambda v: v and "icon" in (v if isinstance(v, list) else [v]))
        if icon and icon.get("href"):
            try:
                url = self.cdn(urljoin(home["url"], icon["href"]))
                r = self.get(url)
                if r.status_code == 200:
                    ext = CT_EXT.get((r.headers.get("content-type") or "").split(";")[0].strip(), ".ico")
                    with open(os.path.join(site_dir, "favicon" + ext), "wb") as f:
                        f.write(r.content)
            except Exception as e:  # noqa: BLE001
                self.err(f"favicon: {e}")

    # ---------- этап 4: таблицы и отчёт ----------
    def url_groups(self):
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        how = {}
        for p, rec in self.pages.items():
            find(p)
            for alt in rec["alternates"].values():
                union(p, alt)
                how[p] = "hreflang"
        # страницы без hreflang (например, магазин) связываем по одинаковому адресу без языка
        by_rest = {}
        for p, rec in self.pages.items():
            if not rec["alternates"]:
                by_rest.setdefault(strip_lang(p), []).append(p)
        for group in by_rest.values():
            for p in group[1:]:
                union(group[0], p)
        comps = {}
        for p in parent:
            comps.setdefault(find(p), []).append(p)
        rows = []
        for members in comps.values():
            row = {lang: [] for lang in LANGS}
            for p in sorted(members):
                row[lang_of(p)].append(p)
            method = "hreflang" if any(how.get(p) for p in members) else "одинаковый адрес"
            rows.append(row | {"how": method})
        rows.sort(key=lambda r: (r["ru"] or r["ua"] or r["en"] or [""])[0])
        return rows

    def write_reports(self):
        out = self.out
        with open(os.path.join(out, "urls.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["язык", "путь", "полный адрес", "код ответа", "редиректы", "куда ведёт редирект",
                        "title", "description", "h1", "canonical", "robots", "og:image",
                        "версия ru", "версия ua", "версия en", "картинок на странице", "файл HTML"])
            for p in sorted(self.pages, key=lambda x: (LANGS.index(lang_of(x)), x)):
                r = self.pages[p]
                a = r["alternates"]
                w.writerow([r["lang"], p, r["url"], r.get("status", ""), r.get("redirects", ""),
                            r.get("final_url", ""), r.get("title", ""), r.get("description", ""),
                            r.get("h1", ""), r.get("canonical", ""), r.get("robots", ""),
                            r.get("og_image", ""), a.get("ru", ""), a.get("ua", ""), a.get("en", ""),
                            len(r.get("image_keys", ())), r.get("html_file", "")])
        groups = self.url_groups()
        with open(os.path.join(out, "url_map.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["ru", "ua", "en", "как связаны"])
            for g in groups:
                w.writerow([" ; ".join(g["ru"]), " ; ".join(g["ua"]), " ; ".join(g["en"]), g["how"]])
        with open(os.path.join(out, "site", "all-urls.txt"), "w", encoding="utf-8") as f:
            for p in sorted(self.pages, key=lambda x: (LANGS.index(lang_of(x)), x)):
                if self.pages[p].get("status") == 200:
                    f.write(self.pages[p]["url"] + "\n")
        with open(os.path.join(out, "images.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["файл", "размер, байт", "ширина", "высота", "тип", "качество", "статус",
                        "откуда скачано", "alt ru", "alt ua", "alt en", "страниц", "страницы (первые 10)",
                        "ошибка"])
            for rec in sorted(self.images.values(), key=lambda r: (r.get("file") or "~", r["id"])):
                pages = sorted(rec["pages"])
                w.writerow([rec.get("file", ""), rec.get("bytes", ""), rec.get("width", ""),
                            rec.get("height", ""), rec.get("content_type", ""), rec.get("quality", ""),
                            rec.get("state", "не запускалось"), rec.get("source_url", ""),
                            rec["alt"].get("ru", ""), rec["alt"].get("ua", ""), rec["alt"].get("en", ""),
                            len(pages), " ; ".join(pages[:10]), rec.get("error", "")])
        self.write_summary(groups)

    def write_summary(self, groups):
        by_lang = {lang: 0 for lang in LANGS}
        ok = {lang: 0 for lang in LANGS}
        bad = []
        for p, r in self.pages.items():
            by_lang[r["lang"]] += 1
            if r.get("status") == 200:
                ok[r["lang"]] += 1
            else:
                bad.append(f"{r.get('status')}  {p}")
        imgs = list(self.images.values())
        got = [r for r in imgs if r.get("file")]
        orig = [r for r in got if r.get("quality") == "оригинал"]
        fallback = [r for r in got if r.get("quality") == "запасная версия"]
        failed = [r for r in imgs if not r.get("file")]
        size = sum(r.get("bytes", 0) for r in got)
        missing_on_pages = []
        for p, r in self.pages.items():
            if r.get("status") != 200:
                continue
            miss = [k for k in r.get("image_keys", ()) if not self.images.get(k, {}).get("file")]
            if miss:
                missing_on_pages.append(f"{p}: не хватает {len(miss)}")
        no_trans = []
        live = {p for p, r in self.pages.items() if r.get("status") == 200}
        for g in groups:
            ru = [p for p in g["ru"] if p in live]
            lacks = [x for x in ("ua", "en") if not any(p in live for p in g[x])]
            if ru and lacks:
                no_trans.append(f"{ru[0]}  (нет: {', '.join(lacks)})")
        lines = [
            f"Выгрузка сайта {self.base}",
            f"Дата: {datetime.now():%Y-%m-%d %H:%M}",
            f"Папка: {self.out}",
            "",
            "СТРАНИЦЫ",
            f"  Всего адресов: {len(self.pages)}  (ru {by_lang['ru']}, ua {by_lang['ua']}, en {by_lang['en']})",
            f"  Открылись (200): ru {ok['ru']}, ua {ok['ua']}, en {ok['en']}",
            f"  Групп страниц (одна страница во всех языках): {len(groups)}",
            "",
            "КАРТИНКИ",
            f"  Уникальных: {len(imgs)}",
            f"  Скачано: {len(got)}  (оригиналов: {len(orig)}, запасных версий: {len(fallback)})",
            f"  Не скачано: {len(failed)}",
            f"  Объём: {human(size)}",
            "",
            "ПРОВЕРКА",
            f"  Страниц, где не хватает картинок: {len(missing_on_pages)}",
        ]
        lines += [f"    {x}" for x in missing_on_pages[:50]]
        lines += ["", f"Адреса с ответом не 200 ({len(bad)}):"] + [f"  {x}" for x in sorted(bad)]
        lines += ["", f"Русские страницы без версии на другом языке ({len(no_trans)}):"]
        lines += [f"  {x}" for x in no_trans]
        lines += ["", f"Ошибки ({len(self.errors)}):"] + [f"  {x}" for x in self.errors[:200]]
        with open(os.path.join(self.out, "report.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.log("\n" + "\n".join(lines[:17]))

    def run(self):
        t0 = time.time()
        self.log(f"Старт. Сайт: {self.base}  Папка: {self.out}")
        self.crawl()
        if self.skip_images:
            self.log("Загрузка картинок пропущена (--skip-images)")
        else:
            self.download_images()
        self.build_content()
        self.write_reports()
        self.log(f"Готово за {int(time.time() - t0)} с. Отчёт: {os.path.join(self.out, 'report.txt')}")
        failed = [r for r in self.images.values() if not r.get("file")]
        return 1 if (failed and not self.skip_images) else 0


class MD:
    """Превращает блок страницы Weblium в Markdown: заголовки, абзацы, списки, ссылки, картинки."""

    def __init__(self, ex, page_url, page_dir):
        self.ex, self.page_url, self.page_dir = ex, page_url, page_dir
        self.lines, self.buf, self.seen_imgs = [], [], set()

    def flush(self):
        text = clean("".join(self.buf))
        self.buf = []
        if text:
            self.lines.append(text)

    def emit_image(self, key, alt):
        if key in self.seen_imgs:
            return
        self.seen_imgs.add(key)
        ref = self.ex.image_ref(key, self.page_dir)
        if ref:
            self.flush()
            alt = alt.replace("[", "(").replace("]", ")")
            self.lines.append(f"![{alt}]({ref})")

    def convert(self, node):
        style = (node.get("style") or "") if isinstance(node, Tag) else ""
        if "url(" in style:  # фон самого блока
            for k in self.ex.keys_in(style):
                self.emit_image(k[3], "")
        self.walk(node)
        self.flush()
        out = []
        for line in self.lines:
            if not out or out[-1] != line:
                out.append(line)
        return out

    def walk(self, node):
        for ch in node.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                self.buf.append(str(ch))
                continue
            if not isinstance(ch, Tag):
                continue
            name = (ch.name or "").lower()
            if name in SKIP_TAGS:
                continue
            style = ch.get("style") or ""
            if "url(" in style:
                for k in self.ex.keys_in(style):
                    self.emit_image(k[3], "")
            if name == "br":
                self.buf.append(" ")
                continue
            if name == "img":
                keys = self.ex.keys_in(attr_blob(ch))
                if keys:
                    self.emit_image(keys[0][3], clean(ch.get("alt") or ch.get("aria-label") or ""))
                continue
            if name in HEADINGS:
                self.flush()
                t = clean(ch.get_text(" "))
                if t:
                    self.lines.append("#" * HEADINGS[name] + " " + t)
                for img in ch.find_all("img"):
                    keys = self.ex.keys_in(attr_blob(img))
                    if keys:
                        self.emit_image(keys[0][3], clean(img.get("alt") or ""))
                continue
            if name == "a":
                link = self.ex.link_target(ch.get("href"), self.page_url)
                complex_ = ch.find(lambda t: isinstance(t, Tag) and (
                    t.name in BLOCK_TAGS or t.name in HEADINGS or t.name == "img"
                    or "url(" in (t.get("style") or "")))
                if not complex_ and "url(" not in style:
                    t = clean(ch.get_text(" "))
                    if t:
                        self.buf.append(f" [{t}]({link}) " if link else f" {t} ")
                    continue
                self.flush()
                self.walk(ch)
                self.flush()
                if link:
                    self.lines.append(f"→ {link}")
                continue
            if name == "li":
                self.flush()
                start = len(self.lines)
                self.walk(ch)
                self.flush()
                if len(self.lines) > start:
                    self.lines[start] = "- " + self.lines[start]
                continue
            if name in ("td", "th"):
                self.buf.append(" | ")
                self.walk(ch)
                continue
            if name in BLOCK_TAGS:
                self.flush()
                self.walk(ch)
                self.flush()
                continue
            self.walk(ch)


def main():
    ap = argparse.ArgumentParser(description="Выгрузка сайта с Weblium: адреса всех языков, HTML, картинки-оригиналы, тексты.")
    ap.add_argument("--site", default="https://dumskaya-design.com", help="адрес сайта")
    ap.add_argument("--out", default="dumskaya-export", help="папка для результата")
    ap.add_argument("--page-workers", type=int, default=4, help="параллельных загрузок страниц")
    ap.add_argument("--image-workers", type=int, default=6, help="параллельных загрузок картинок")
    ap.add_argument("--content-langs", default="ru", help="для каких языков делать content.md, например ru,ua,en")
    ap.add_argument("--max-pages", type=int, default=3000, help="предохранитель от бесконечного обхода")
    ap.add_argument("--skip-images", action="store_true", help="не качать картинки (быстрая проверка адресов)")
    ap.add_argument("--cdn-override", action="append", default=[], help=argparse.SUPPRESS)
    args = ap.parse_args()
    try:
        code = Exporter(args).run()
    except KeyboardInterrupt:
        print("\nОстановлено. Запустите ещё раз — скачанное сохранится, докачается остальное.")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
