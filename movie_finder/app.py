import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
from bs4 import BeautifulSoup
from urllib.parse import quote
from datetime import datetime
import pykakasi

app = Flask(__name__)

_kks = pykakasi.kakasi()

def get_reading(text):
    """漢字混じりテキストをひらがな読みに変換（例: 銀魂 → ぎんたま）"""
    return ''.join(item['hira'] for item in _kks.convert(text))

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
}

PREFECTURES = [
    ('01','北海道'), ('02','青森'), ('03','岩手'), ('04','宮城'), ('05','秋田'),
    ('06','山形'), ('07','福島'), ('08','茨城'), ('09','栃木'), ('10','群馬'),
    ('11','埼玉'), ('12','千葉'), ('13','東京'), ('14','神奈川'), ('15','新潟'),
    ('16','富山'), ('17','石川'), ('18','福井'), ('19','山梨'), ('20','長野'),
    ('21','岐阜'), ('22','静岡'), ('23','愛知'), ('24','三重'), ('25','滋賀'),
    ('26','京都'), ('27','大阪'), ('28','兵庫'), ('29','奈良'), ('30','和歌山'),
    ('31','鳥取'), ('32','島根'), ('33','岡山'), ('34','広島'), ('35','山口'),
    ('36','徳島'), ('37','香川'), ('38','愛媛'), ('39','高知'), ('40','福岡'),
    ('41','佐賀'), ('42','長崎'), ('43','熊本'), ('44','大分'), ('45','宮崎'),
    ('46','鹿児島'), ('47','沖縄'),
]

# 都道府県ごとのキャッシュ（エリア・映画）
_pref_cache = {}       # {pref_code: {'data': ..., 'ts': float}}
CACHE_TTL = 6 * 3600  # 6時間でキャッシュ失効 → 自動で最新データを再取得


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.encoding = 'utf-8'
    return BeautifulSoup(r.text, 'html.parser')


def pref_full_name(pref_code):
    """都道府県コードから正式名称を返す（例：'11' → '埼玉県'）"""
    name = dict(PREFECTURES).get(pref_code, '')
    if name == '北海道': return '北海道'
    if name == '東京': return '東京都'
    if name in ('大阪', '京都'): return name + '府'
    return name + '県' if name else ''


def extract_city(text, pref_full=''):
    """住所テキストから市区町村名を抽出。〒直後パターン優先・ナビテキスト除外。"""
    def valid(c):
        return c and len(c) <= 10 and not re.search(r'[\s　>〉]', c) and '映画' not in c

    def clean(c, pf):
        # 都道府県名が先頭に残っていたら除去（例: 東京都渋谷区 → 渋谷区）
        if pf and c.startswith(pf):
            c = c[len(pf):]
        return c

    # 〒直後の正式住所から抽出（最優先）
    pat_zip = r'〒\d{3}[-－]\d{4}[^\n]{0,20}?'
    if pref_full:
        m = re.search(pat_zip + re.escape(pref_full) + r'([\S]{1,10}?[市区町村])', text)
        if m and valid(clean(m.group(1), pref_full)):
            return clean(m.group(1), pref_full)

    m = re.search(pat_zip + r'[都道府県]([\S]{1,10}?[市区町村])', text)
    if m and valid(m.group(1)):
        return m.group(1)

    # 都道府県名からの抽出（全マッチを試してvalidなものを返す）
    if pref_full:
        for m in re.finditer(re.escape(pref_full) + r'([\S]{1,10}?[市区町村])', text):
            c = clean(m.group(1), pref_full)
            if valid(c):
                return c

    for m in re.finditer(r'[都道府県]([\S]{1,10}?[市区町村])', text):
        if valid(m.group(1)):
            return m.group(1)
    return ''


def fetch_area_info(pref_code, area_code):
    """エリアページから映画館IDを取得"""
    try:
        soup = fetch(f'https://eiga.com/theater/{pref_code}/{area_code}/')
        cinema_ids = []
        seen = set()
        for link in soup.select('a[href]'):
            href = link.get('href', '')
            m = re.fullmatch(r'/theater/(\d+)/(\d+)/(\d+)/', href)
            if m and m.group(2) == area_code and m.group(3) not in seen:
                seen.add(m.group(3))
                cinema_ids.append(m.group(3))
        return {'area_code': area_code, 'cinema_ids': cinema_ids}
    except Exception:
        return {'area_code': area_code, 'cinema_ids': []}


def fetch_cinema_movies(pref_code, area_code, cinema_id):
    """映画館ページから上映中の全映画と市区町村名を取得"""
    pref_full = pref_full_name(pref_code)
    skip = {'作品情報', '映画館を探す', 'もっと見る'}
    try:
        soup = fetch(f'https://eiga.com/theater/{pref_code}/{area_code}/{cinema_id}/')
        text = soup.get_text()
        city = extract_city(text, pref_full)
        movies = []
        seen = set()
        for link in soup.select('a[href]'):
            m = re.fullmatch(r'/movie/(\d+)/', link.get('href', ''))
            if m:
                mid = m.group(1)
                title = link.get_text(strip=True)
                if mid not in seen and title and title not in skip and len(title) > 1:
                    seen.add(mid)
                    movies.append({'id': mid, 'title': title})
        return {'city': city, 'movies': movies}
    except Exception:
        return {'city': '', 'movies': []}


def get_pref_data(pref_code):
    """都道府県のエリア一覧（市区町村名）と上映中映画を取得・キャッシュ（6時間で自動更新）"""
    cached = _pref_cache.get(pref_code)
    if cached and time.time() - cached['ts'] < CACHE_TTL:
        return cached['data']

    # エリアコード一覧を取得（1リクエスト）
    soup = fetch(f'https://eiga.com/theater/{pref_code}/')
    area_codes = []
    seen = set()
    for link in soup.select('a[href]'):
        href = link.get('href', '')
        m = re.fullmatch(r'/theater/(\d+)/(\d+)/', href)
        if m and m.group(1) == str(int(pref_code)) and m.group(2) not in seen:
            seen.add(m.group(2))
            area_codes.append(m.group(2))

    # 各エリアページを並列取得（市区町村名 + 映画館ID）
    with ThreadPoolExecutor(max_workers=3) as ex:
        area_results = list(ex.map(lambda c: fetch_area_info(pref_code, c), area_codes))

    # 市区町村名をまとめ、映画館リストを収集
    city_set = []
    seen_cities = set()
    cinema_list = []  # (area_code, cinema_id)

    for r in area_results:
        for cid in r['cinema_ids']:
            cinema_list.append((r['area_code'], cid))

    # 各映画館ページから全上映映画と市区町村名を並列取得
    movie_map = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        cinema_results = list(ex.map(
            lambda ac: fetch_cinema_movies(pref_code, ac[0], ac[1]),
            cinema_list
        ))
    for result in cinema_results:
        city = result['city']
        if city and city not in seen_cities:
            seen_cities.add(city)
            city_set.append(city)
        for mv in result['movies']:
            if mv['id'] not in movie_map:
                movie_map[mv['id']] = mv['title']

    movies = sorted(
        [{'id': mid, 'title': title, 'reading': get_reading(title)}
         for mid, title in movie_map.items()],
        key=lambda m: m['title']
    )
    data = {
        'cities': sorted(city_set),
        'movies': movies,
    }
    _pref_cache[pref_code] = {'data': data, 'ts': time.time()}
    return data


def search_movie(query):
    """映画を検索してIDとタイトルを返す"""
    soup = fetch(f'https://eiga.com/search/{quote(query)}/')
    for link in soup.select('a[href]'):
        m = re.fullmatch(r'/movie/(\d+)/', link.get('href', ''))
        if m:
            return m.group(1), link.get_text(strip=True)
    return None, None


def get_movie_duration(movie_id):
    """映画の上映時間（分）を取得"""
    soup = fetch(f'https://eiga.com/movie/{movie_id}/')
    text = soup.get_text()
    m = re.search(r'上映時間[：:]\s*(\d+)分', text)
    return int(m.group(1)) if m else 120


def get_cinemas_with_areas(movie_id, pref_code):
    """映画の上映館リストを取得（市区町村名付き）"""
    pref_full = pref_full_name(pref_code)
    soup = fetch(f'https://eiga.com/movie-pref/{movie_id}/{pref_code}/')
    cinemas = []
    seen = set()
    cinema_re = re.compile(r'(/movie-theater/\d+/\d+/(\d+)/\d+/)')

    dl = soup.find('dl', class_='theater-area-list')
    if not dl:
        return cinemas

    for child in dl.children:
        if not hasattr(child, 'name') or child.name is None:
            continue
        if child.name == 'dd':
            link = child.find('a', href=cinema_re)
            if not link:
                continue
            href = link.get('href', '')
            if href in seen:
                continue
            seen.add(href)
            m = cinema_re.search(href)
            area_code = m.group(2) if m else ''

            # 住所から市区町村を抽出
            full_text = child.get_text(strip=True)
            cinema_name = link.get_text(strip=True)
            address_part = full_text[len(cinema_name):]
            city = extract_city(address_part, pref_full)

            cinemas.append({
                'name': cinema_name,
                'url': f'https://eiga.com{href}',
                'area_code': area_code,
                'area_name': city,
            })

    return cinemas


def date_input_to_page_format(date_str):
    """'2026-03-16' → '3/16' に変換"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return f'{dt.month}/{dt.day}'
    except Exception:
        return None


def time_to_min(t):
    """HH:MM を分に変換"""
    parts = t.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def parse_showtimes(cinema_url, movie_duration, avail_start, avail_end, date_prefix=None):
    """上映時間を取得してフィルタリング"""
    soup = fetch(cinema_url)
    tds = soup.select('.weekly-schedule td')

    avail_start_min = time_to_min(avail_start)
    avail_end_min = time_to_min(avail_end)
    time_pattern = re.compile(r'^\d{1,2}:\d{2}')

    results = []
    for td in tds:
        date_el = td.find('p')
        date = date_el.get_text(strip=True) if date_el else ''

        if date_prefix and date_prefix not in date:
            continue

        slots = [el.get_text(strip=True) for el in td.find_all(['a', 'span'])]
        slots = [s for s in slots if time_pattern.match(s)]

        matched_times = []
        for slot in slots:
            if '～' in slot:
                start_str, end_str = slot.split('～')
            else:
                start_str = slot
                end_str = None

            start_min = time_to_min(start_str)
            end_min = time_to_min(end_str) if end_str else start_min + movie_duration

            if start_min >= avail_start_min and end_min <= avail_end_min:
                end_display = end_str if end_str else f'{end_min // 60}:{end_min % 60:02d}'
                matched_times.append({'start': start_str, 'end': end_display})

        if matched_times:
            results.append({'date': date, 'times': matched_times})

    return results


# ─── APIエンドポイント ───────────────────────────────────────────

@app.route('/api/pref_data')
def api_pref_data():
    """都道府県のエリア（市区町村）と上映中映画を返す"""
    pref_code = request.args.get('pref', '11')
    try:
        data = get_pref_data(pref_code)
        return jsonify(data)
    except Exception as e:
        return jsonify({'cities': [], 'movies': [], 'error': str(e)})


@app.route('/')
def index():
    return render_template('index.html', prefectures=PREFECTURES)


@app.route('/search', methods=['POST'])
def search():
    movie_query = request.form.get('movie', '').strip()
    pref_code = request.form.get('prefecture', '11')
    avail_start = request.form.get('avail_start', '09:00')
    avail_end = request.form.get('avail_end', '21:00')
    date_str = request.form.get('date', '').strip()
    city_filters = [c for c in request.form.getlist('city') if c.strip()]

    if not movie_query:
        return jsonify({'error': '映画名を入力してください'})

    movie_id, movie_title = search_movie(movie_query)
    if not movie_id:
        return jsonify({'error': f'「{movie_query}」が見つかりませんでした'})

    duration = get_movie_duration(movie_id)
    date_prefix = date_input_to_page_format(date_str) if date_str else None

    cinemas = get_cinemas_with_areas(movie_id, pref_code)
    if not cinemas:
        pref_name = dict(PREFECTURES).get(pref_code, '')
        return jsonify({'error': f'{pref_name}では上映している映画館が見つかりませんでした'})

    # 市区町村フィルター（複数選択対応）
    if city_filters:
        city_set = set(city_filters)
        cinemas = [c for c in cinemas if c['area_name'] in city_set]

    matched_cinemas = []
    for cinema in cinemas:
        try:
            showtimes = parse_showtimes(
                cinema['url'], duration, avail_start, avail_end, date_prefix
            )
            if showtimes:
                matched_cinemas.append({
                    'name': cinema['name'],
                    'url': cinema['url'],
                    'area_name': cinema['area_name'],
                    'showtimes': showtimes,
                })
        except Exception:
            continue

    pref_name = dict(PREFECTURES).get(pref_code, '')

    return jsonify({
        'movie_title': movie_title,
        'duration': duration,
        'prefecture': pref_name,
        'avail_start': avail_start,
        'avail_end': avail_end,
        'date_label': date_prefix or '全日程',
        'city_filter': '・'.join(city_filters) if city_filters else '',
        'cinemas': matched_cinemas,
        'total_cinemas': len(cinemas),
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
