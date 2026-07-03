#!/usr/bin/env python3
"""Xops ダイジェスト生成スクリプト（Python 3 標準ライブラリのみ）。

LLM の役割は要約テキスト執筆のみに限定し、取得・日付窓計算・XML 生成・
検証・剪定といった決定的処理はすべて本スクリプトが担う。

サブコマンド:
  collect : S3 の元 RSS を取得し、カットオフより新しい投稿を digest_work/input.json に集約
  render  : digest_work/summaries.json を読み、digest/<slug>.xml へ item を冪等追加・剪定・検証
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.dom.minidom
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from xml.sax.saxutils import escape

# --- 定数 ---------------------------------------------------------------
# JST（元フィードは UTC 表記だが、ダイジェストの日付・午前/午後判定は JST 基準）
JST = timezone(timedelta(hours=9))
# GitHub Pages の公開ベース URL（channel link / atom:self に使用）
PAGES_BASE = "https://codet2git.github.io/xops-rss-feed"
# リポジトリルート（本スクリプトは scripts/ 配下に置かれる前提）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "digest_config.json")
DIGEST_DIR = os.path.join(ROOT, "digest")
WORK_DIR = os.path.join(ROOT, "digest_work")
# description 末尾のエンゲージメント表記 [like=N repost=M] を抽出する正規表現
ENGAGEMENT_RE = re.compile(r"\s*\[like=(\d+)\s+repost=(\d+)\]\s*$")
# title 先頭の @username: からユーザー名を拾うフォールバック用
AUTHOR_FROM_TITLE_RE = re.compile(r"^@([A-Za-z0-9_]+):")


def load_config():
    """digest_config.json を読み込んで dict を返す。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_pubdate(text):
    """RFC822 の pubDate 文字列を aware datetime に変換（失敗時 None）。"""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    # tz 情報が欠落している場合は UTC とみなして比較可能にする
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def split_engagement(description):
    """description を本文とエンゲージメント数に分離し (text, likes, reposts) を返す。"""
    if description is None:
        return "", 0, 0
    m = ENGAGEMENT_RE.search(description)
    if m:
        likes = int(m.group(1))
        reposts = int(m.group(2))
        text = description[: m.start()].rstrip()
    else:
        # 末尾表記が無い場合は全文を本文とし、数値は 0
        likes = reposts = 0
        text = description.strip()
    return text, likes, reposts


def latest_digest_pubdate(slug):
    """既存 digest/<slug>.xml の最新 item pubDate（aware datetime）。無ければ None。"""
    path = os.path.join(DIGEST_DIR, f"{slug}.xml")
    if not os.path.exists(path):
        return None
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    channel = tree.getroot().find("channel")
    if channel is None:
        return None
    latest = None
    for item in channel.findall("item"):
        dt = parse_pubdate(item.findtext("pubDate"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def fetch_feed(url):
    """フィード URL を timeout 30s で取得し bytes を返す（例外は呼び出し側で処理）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "xops-digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def reject_dtd(raw_bytes):
    """外部フィードに DOCTYPE / ENTITY 宣言があれば拒否する（XXE・billion-laughs 対策）。

    標準ライブラリの XML パーサーは外部エンティティ展開・DTD 取得こそ既定で無効だが、
    内部エンティティの再帰展開（billion-laughs）は依然脆弱。エンティティ定義は
    DOCTYPE 内部サブセットにしか置けないため、DOCTYPE/ENTITY を含む入力を
    パース前に弾くことで多層防御とする（正当な RSS 2.0 は DTD を持たない）。
    """
    head = raw_bytes[:65536]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw_bytes:
        raise ValueError("DTD/ENTITY を含むフィードは拒否しました")


def parse_source_items(raw_bytes, cutoff):
    """元 RSS の bytes をパースし、cutoff より新しい投稿の dict リストを返す。"""
    # 信頼できない外部入力なのでパース前に DTD/ENTITY を拒否
    reject_dtd(raw_bytes)
    root = ET.fromstring(raw_bytes)
    channel = root.find("channel")
    posts = []
    if channel is None:
        return posts
    for item in channel.findall("item"):
        dt = parse_pubdate(item.findtext("pubDate"))
        # カットオフ以前・日付不明の投稿は除外
        if dt is None or dt <= cutoff:
            continue
        description = item.findtext("description") or ""
        text, likes, reposts = split_engagement(description)
        # author 要素を優先し、無ければ title の @username: から補完
        author = item.findtext("author")
        if not author:
            title = item.findtext("title") or ""
            m = AUTHOR_FROM_TITLE_RE.match(title.strip())
            author = m.group(1) if m else ""
        posts.append({
            "author": author,
            "text": text,
            "likes": likes,
            "reposts": reposts,
            "url": item.findtext("link") or "",
            "at": dt.astimezone(JST).isoformat(),
        })
    return posts


def cmd_collect(config):
    """collect: 全フィードを取得しカットオフ以降の投稿を digest_work/input.json に書き出す。"""
    min_posts = config.get("min_posts", 5)
    now = datetime.now(JST)
    feeds_out = []
    for feed in config["feeds"]:
        slug = feed["slug"]
        name = feed["name"]
        # カットオフ = 既存ダイジェストの最新 item pubDate、無ければ now-24h
        cutoff = latest_digest_pubdate(slug) or (now - timedelta(hours=24))
        entry = {"slug": slug, "name": name, "skip": False,
                 "reason": None, "post_count": 0, "posts": []}
        try:
            raw = fetch_feed(feed["url"])
            posts = parse_source_items(raw, cutoff)
        except (urllib.error.URLError, urllib.error.HTTPError,
                ET.ParseError, TimeoutError, ValueError, OSError) as e:
            # 取得・パース失敗はスキップ扱いにしてプロセス全体は継続させる
            entry["skip"] = True
            entry["reason"] = f"取得失敗: {type(e).__name__}: {e}"
            feeds_out.append(entry)
            continue
        entry["posts"] = posts
        entry["post_count"] = len(posts)
        # 新規投稿が min_posts 未満ならスキップ（posts はそのまま残す）
        if len(posts) < min_posts:
            entry["skip"] = True
            entry["reason"] = f"投稿数不足: {len(posts)} < {min_posts}"
        feeds_out.append(entry)

    os.makedirs(WORK_DIR, exist_ok=True)
    out = {"generated_at": now.isoformat(), "feeds": feeds_out}
    with open(os.path.join(WORK_DIR, "input.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # cloud agent のログ用に 1 行サマリーを標準出力へ
    for e in feeds_out:
        state = "skip" if e["skip"] else "ok"
        suffix = f" ({e['reason']})" if e["reason"] else ""
        print(f"[{state}] {e['slug']}: {e['post_count']} posts{suffix}")
    return 0


def wrap_cdata(html):
    """html を CDATA で包む。]]> を含む場合は分割してエスケープする。"""
    # CDATA セクション内で ]]> を安全に表現するための分割エスケープ
    safe = html.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def build_item_xml(item):
    """item dict から <item> 要素文字列を生成（title はエスケープ、html は CDATA）。"""
    return (
        "    <item>\n"
        f"      <title>{escape(item['title'])}</title>\n"
        f'      <guid isPermaLink="false">{escape(item["guid"])}</guid>\n'
        f"      <pubDate>{item['pubDate']}</pubDate>\n"
        f"      <description>{wrap_cdata(item['html'])}</description>\n"
        "    </item>\n"
    )


def build_channel_xml(slug, name, items, last_build_dt):
    """channel メタデータ（config 由来）+ items から RSS 2.0 全文文字列を生成する。"""
    link = f"{PAGES_BASE}/digest/{slug}.xml"
    title = f"Xops Digest - {name}"
    description = f"{name} リストのAIトレンドダイジェスト（朝夕2回更新）"
    items_xml = "".join(build_item_xml(it) for it in items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(link)}</link>\n"
        f'    <atom:link href="{escape(link)}" rel="self" type="application/rss+xml"/>\n'
        f"    <description>{escape(description)}</description>\n"
        "    <language>ja</language>\n"
        f"    <lastBuildDate>{format_datetime(last_build_dt)}</lastBuildDate>\n"
        f"{items_xml}"
        "  </channel>\n"
        "</rss>\n"
    )


def read_existing_items(slug):
    """既存 digest/<slug>.xml の item を dict リストで返す（CDATA は透過的に text 化される）。"""
    path = os.path.join(DIGEST_DIR, f"{slug}.xml")
    items = []
    if not os.path.exists(path):
        return items
    tree = ET.parse(path)
    channel = tree.getroot().find("channel")
    if channel is None:
        return items
    for item in channel.findall("item"):
        items.append({
            "guid": item.findtext("guid") or "",
            "title": item.findtext("title") or "",
            "pubDate": item.findtext("pubDate") or "",
            "html": item.findtext("description") or "",
        })
    return items


def sort_key_pubdate(item):
    """item を pubDate 降順に並べるためのソートキー（パース不能は最古扱い）。"""
    dt = parse_pubdate(item.get("pubDate"))
    return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)


def cmd_render(config):
    """render: summaries.json を読み digest/<slug>.xml へ item を冪等追加・剪定・検証する。"""
    keep_items = config.get("keep_items", 14)
    summaries_path = os.path.join(WORK_DIR, "summaries.json")
    # summaries.json の不存在・JSON 不正は失敗
    try:
        with open(summaries_path, encoding="utf-8") as f:
            summaries = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"summaries.json 読込失敗: {e}", file=sys.stderr)
        return 1
    if not isinstance(summaries, dict):
        print("summaries.json は {slug: {...}} 形式である必要があります", file=sys.stderr)
        return 1

    config_names = {feed["slug"]: feed["name"] for feed in config["feeds"]}
    now = datetime.now(JST)
    # guid 用の日付・午前/午後（JST 12 時前=am、以降=pm）
    ampm = "am" if now.hour < 12 else "pm"
    date_tag = now.strftime("%Y%m%d")
    pubdate_str = format_datetime(now)

    rendered = []
    for slug, payload in summaries.items():
        if slug not in config_names:
            # config 未登録の slug は scope 外として無視（対象 XML は無変更）
            print(f"警告: config 未登録の slug をスキップ: {slug}", file=sys.stderr)
            continue
        # payload の妥当性チェック（title / html 必須、html 空文字は失敗）
        if not isinstance(payload, dict):
            print(f"summaries[{slug}] は object である必要があります", file=sys.stderr)
            return 1
        title = payload.get("title")
        html = payload.get("html")
        if not title or not html:
            print(f"summaries[{slug}]: title または html が空です", file=sys.stderr)
            return 1

        name = config_names[slug]
        guid = f"digest-{slug}-{date_tag}-{ampm}"
        new_item = {"guid": guid, "title": title,
                    "pubDate": pubdate_str, "html": html}

        # 既存 item から同一 guid を除去してから新 item を追加（冪等性の担保）
        items = [it for it in read_existing_items(slug) if it["guid"] != guid]
        items.append(new_item)
        # pubDate 降順に並べ、keep_items で剪定（古い item から落ちる）
        items.sort(key=sort_key_pubdate, reverse=True)
        items = items[:keep_items]

        xml_str = build_channel_xml(slug, name, items, now)
        # 書き込み前に全文を minidom で検証（失敗時は書かず exit 1）
        try:
            xml.dom.minidom.parseString(xml_str)
        except Exception as e:  # noqa: BLE001 - 検証失敗は種別を問わず中止する
            print(f"{slug}: 生成 XML の検証失敗のため中止: {e}", file=sys.stderr)
            return 1
        rendered.append((slug, xml_str, len(items)))

    # 全 slug が検証を通過してから一括書き込み（部分適用を避ける）
    for slug, xml_str, count in rendered:
        path = os.path.join(DIGEST_DIR, f"{slug}.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml_str)
        print(f"[rendered] {slug}: {count} items")
    return 0


def main():
    """サブコマンド collect / render を振り分けるエントリポイント。"""
    parser = argparse.ArgumentParser(description="Xops ダイジェスト生成")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="元 RSS を取得し input.json を生成")
    sub.add_parser("render", help="summaries.json から digest XML を生成")
    args = parser.parse_args()
    config = load_config()
    if args.command == "collect":
        return cmd_collect(config)
    if args.command == "render":
        return cmd_render(config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
