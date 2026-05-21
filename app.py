# app.py
# ============================================
# App Store Reviews Scraper для Streamlit
# Регион: ru
# Источники:
# 1) официальный Apple RSS endpoint;
# 2) неофициальный HTTP fallback без Selenium/Playwright.
#
# Запуск:
# streamlit run app.py
#
# Важно:
# App Store может ограничивать количество публично доступных отзывов.
# Неофициальные endpoints Apple нестабильны и могут перестать работать,
# если Apple изменит внутренние API или правила доступа.
# ============================================

import re
import time
import random
import html
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import streamlit as st


# -----------------------------
# Базовые настройки
# -----------------------------

COUNTRY = "ru"
LOOKBACK_DAYS = 365

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

CSV_COLUMNS = [
    "ID",
    "Название приложения",
    "Рейтинг",
    "Дата",
    "Автор",
    "Заголовок",
    "Текст отзыва",
    "Страна отзыва",
    "Версия приложения",
]


# -----------------------------
# Вспомогательные функции
# -----------------------------

def safe_text(value):
    """Безопасно приводит значение к строке."""
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("label") or value.get("value") or ""
    return html.unescape(str(value)).strip()


def get_nested(data, path, default=None):
    """Безопасно достаёт вложенное значение из dict/list."""
    current = data

    for key in path:
        try:
            if isinstance(current, dict):
                current = current.get(key, default)
            elif isinstance(current, list) and isinstance(key, int):
                current = current[key]
            else:
                return default
        except Exception:
            return default

        if current is None:
            return default

    return current


def parse_date(date_value):
    """Парсит дату из разных форматов Apple."""
    if not date_value:
        return None

    date_value = str(date_value).strip()

    try:
        normalized = date_value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass

    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(date_value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            continue

    return None


def format_date_for_csv(dt):
    """Форматирует дату для CSV."""
    if not dt:
        return ""

    if isinstance(dt, str):
        dt = parse_date(dt)

    if not dt:
        return ""

    return dt.strftime("%Y-%m-%d")


def request_json(url, headers=None, params=None, timeout=20):
    """HTTP GET с обработкой ошибок и JSON-ответом."""
    final_headers = dict(HEADERS)

    if headers:
        final_headers.update(headers)

    try:
        response = requests.get(
            url,
            headers=final_headers,
            params=params,
            timeout=timeout,
        )

        if response.status_code == 404:
            raise RuntimeError(
                "Apple вернул 404: ресурс не найден или недоступен для региона ru."
            )

        if response.status_code == 429:
            raise RuntimeError(
                "Apple временно ограничил частоту запросов. Попробуйте позже."
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Ошибка HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            return response.json()
        except Exception:
            raise RuntimeError("Apple вернул ответ не в формате JSON.")

    except requests.exceptions.Timeout:
        raise RuntimeError("Превышено время ожидания ответа от Apple.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Ошибка соединения. Проверьте интернет-подключение.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Ошибка запроса: {e}")


# -----------------------------
# Основные функции
# -----------------------------

def extract_app_id(app_url):
    """
    Извлекает App Store ID из ссылки вида:
    https://apps.apple.com/us/app/duolingo-language-lessons/id570060128
    или:
    https://apps.apple.com/us/app/duolingo-language-lessons/id570060128?l=ru
    """
    if not app_url or not str(app_url).strip():
        raise ValueError("Вставьте ссылку на приложение App Store.")

    app_url = str(app_url).strip()

    match = re.search(r"/id(\d+)", app_url)
    if not match:
        match = re.search(r"id(\d+)", app_url)

    if not match:
        raise ValueError(
            "Не удалось извлечь App Store ID. "
            "Проверьте, что ссылка содержит фрагмент вида id570060128."
        )

    return match.group(1)


def get_app_name(app_id, country="ru"):
    """Получает название приложения через Apple Lookup API."""
    lookup_url = "https://itunes.apple.com/lookup"

    params = {
        "id": app_id,
        "country": country,
        "entity": "software",
    }

    data = request_json(lookup_url, params=params)

    result_count = data.get("resultCount", 0)
    results = data.get("results", [])

    if result_count < 1 or not results:
        raise RuntimeError(
            "Приложение не найдено через Apple Lookup API. "
            "Возможно, оно недоступно в российском App Store."
        )

    app_name = results[0].get("trackName") or results[0].get("trackCensoredName")

    if not app_name:
        raise RuntimeError("Apple Lookup API не вернул название приложения.")

    return safe_text(app_name)


def normalize_review(raw_review, source, app_id, app_name, country):
    """
    Приводит отзыв из RSS или неофициального endpoint к единому формату.
    """
    if source == "rss":
        review_id = safe_text(get_nested(raw_review, ["id", "label"]))
        rating = safe_text(get_nested(raw_review, ["im:rating", "label"]))
        date_raw = safe_text(get_nested(raw_review, ["updated", "label"]))
        author = safe_text(get_nested(raw_review, ["author", "name", "label"]))
        title = safe_text(get_nested(raw_review, ["title", "label"]))
        text = safe_text(get_nested(raw_review, ["content", "label"]))
        version = safe_text(get_nested(raw_review, ["im:version", "label"]))

    elif source == "unofficial":
        attributes = raw_review.get("attributes", {}) if isinstance(raw_review, dict) else {}

        review_id = safe_text(raw_review.get("id"))
        rating = safe_text(attributes.get("rating"))
        date_raw = safe_text(attributes.get("date"))
        author = safe_text(
            attributes.get("userName")
            or attributes.get("name")
            or attributes.get("nickname")
        )
        title = safe_text(attributes.get("title"))
        text = safe_text(
            attributes.get("review")
            or attributes.get("body")
            or attributes.get("content")
        )
        version = safe_text(
            attributes.get("appVersionString")
            or attributes.get("version")
            or attributes.get("appVersion")
        )

    else:
        review_id = ""
        rating = ""
        date_raw = ""
        author = ""
        title = ""
        text = ""
        version = ""

    parsed_date = parse_date(date_raw)

    if not review_id:
        base = f"{app_id}|{date_raw}|{author}|{title}|{text}"
        review_id = hashlib.md5(base.encode("utf-8")).hexdigest()

    return {
        "ID": review_id,
        "Название приложения": app_name,
        "Рейтинг": rating,
        "Дата": format_date_for_csv(parsed_date),
        "Автор": author,
        "Заголовок": title,
        "Текст отзыва": text,
        "Страна отзыва": country,
        "Версия приложения": version,
        "_parsed_date": parsed_date,
        "_source": source,
    }


def fetch_reviews_rss(app_id, country="ru", app_name=None, status_callback=None):
    """
    Собирает отзывы через официальный Apple RSS endpoint.
    Apple обычно отдаёт ограниченное количество страниц.
    """
    reviews = []
    max_pages = 10

    for page in range(1, max_pages + 1):
        url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/json"
        )

        try:
            data = request_json(url)
        except Exception as e:
            if page == 1 and status_callback:
                status_callback(f"⚠️ RSS endpoint недоступен: {e}")
            break

        feed = data.get("feed", {})
        entries = feed.get("entry", [])

        if not entries:
            break

        if isinstance(entries, dict):
            entries = [entries]

        page_reviews = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # В RSS первая запись иногда является карточкой приложения, а не отзывом.
            has_rating = get_nested(entry, ["im:rating", "label"]) is not None
            has_content = get_nested(entry, ["content", "label"]) is not None
            has_author = get_nested(entry, ["author", "name", "label"]) is not None

            if not (has_rating and has_content and has_author):
                continue

            page_reviews.append(
                normalize_review(
                    raw_review=entry,
                    source="rss",
                    app_id=app_id,
                    app_name=app_name or "",
                    country=country,
                )
            )

        reviews.extend(page_reviews)

        if status_callback:
            status_callback(f"RSS: страница {page}, получено отзывов: {len(page_reviews)}")

        if not page_reviews:
            break

        time.sleep(random.uniform(0.3, 0.9))

    return reviews


def fetch_reviews_unofficial(app_id, country="ru", app_name=None, status_callback=None):
    """
    Fallback-метод через неофициальный HTTP endpoint Apple.
    Не использует Selenium, Playwright или браузерную автоматизацию.

    Важно:
    этот endpoint не является официально документированным API для отзывов.
    Он может вернуть 401/403 или перестать работать при изменениях Apple.
    """
    reviews = []

    base_url = f"https://amp-api.apps.apple.com/v1/catalog/{country}/apps/{app_id}/reviews"

    unofficial_headers = {
        "Origin": "https://apps.apple.com",
        "Referer": f"https://apps.apple.com/{country}/app/id{app_id}",
        "Accept": "application/json",
    }

    limit = 20
    max_offsets = 25

    for i in range(max_offsets):
        offset = i * limit

        params = {
            "l": "ru-RU",
            "offset": offset,
            "limit": limit,
            "platform": "web",
            "additionalPlatforms": "appletv,ipad,iphone,mac",
        }

        try:
            data = request_json(
                base_url,
                headers=unofficial_headers,
                params=params,
                timeout=20,
            )
        except Exception as e:
            if i == 0 and status_callback:
                status_callback(
                    f"⚠️ Неофициальный fallback недоступен или заблокирован Apple: {e}"
                )
            break

        items = data.get("data", [])

        if not items:
            break

        page_reviews = []

        for item in items:
            if not isinstance(item, dict):
                continue

            page_reviews.append(
                normalize_review(
                    raw_review=item,
                    source="unofficial",
                    app_id=app_id,
                    app_name=app_name or "",
                    country=country,
                )
            )

        reviews.extend(page_reviews)

        if status_callback:
            status_callback(
                f"Fallback: offset {offset}, получено отзывов: {len(page_reviews)}"
            )

        if len(items) < limit:
            break

        time.sleep(random.uniform(0.5, 1.0))

    return reviews


def filter_reviews_last_year(reviews):
    """Оставляет только отзывы за последние 365 дней."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    filtered = []

    for review in reviews:
        dt = review.get("_parsed_date")

        if isinstance(dt, str):
            dt = parse_date(dt)

        if dt and dt >= cutoff:
            filtered.append(review)

    return filtered


def deduplicate_reviews(reviews):
    """
    Удаляет дубли по ID, а при отсутствии ID —
    по комбинации дата + автор + заголовок + текст.
    """
    seen = set()
    unique = []

    for review in reviews:
        review_id = safe_text(review.get("ID"))

        if review_id:
            key = f"id::{review_id}"
        else:
            key_base = "|".join([
                safe_text(review.get("Дата")),
                safe_text(review.get("Автор")),
                safe_text(review.get("Заголовок")),
                safe_text(review.get("Текст отзыва")),
            ])
            key = "hash::" + hashlib.md5(key_base.encode("utf-8")).hexdigest()

        if key in seen:
            continue

        seen.add(key)
        unique.append(review)

    return unique


def reviews_to_dataframe(reviews):
    """Преобразует отзывы в DataFrame только с нужными CSV-колонками."""
    clean_reviews = []

    for review in reviews:
        clean_reviews.append({
            column: review.get(column, "")
            for column in CSV_COLUMNS
        })

    return pd.DataFrame(clean_reviews, columns=CSV_COLUMNS)


def dataframe_to_csv_bytes(df):
    """
    Возвращает CSV в байтах с BOM utf-8-sig,
    чтобы файл корректно открывался в Excel.
    """
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def run_scraper(app_url, status_callback=None):
    """Главная функция запуска скрейпера."""
    if status_callback is None:
        status_callback = lambda message: None

    status_callback("🚀 Запуск сбора отзывов App Store")
    status_callback("Регион отзывов: ru")
    status_callback("Период: последние 12 месяцев")

    app_id = extract_app_id(app_url)
    status_callback(f"✅ App Store ID найден: {app_id}")

    app_name = get_app_name(app_id, country=COUNTRY)
    status_callback(f"✅ Приложение найдено: {app_name}")

    status_callback("📡 Шаг 1: сбор отзывов через официальный Apple RSS endpoint...")
    rss_reviews = fetch_reviews_rss(
        app_id=app_id,
        country=COUNTRY,
        app_name=app_name,
        status_callback=status_callback,
    )
    status_callback(f"✅ RSS: всего получено отзывов до фильтрации: {len(rss_reviews)}")

    status_callback("📡 Шаг 2: попытка сбора через неофициальный HTTP fallback...")
    fallback_reviews = fetch_reviews_unofficial(
        app_id=app_id,
        country=COUNTRY,
        app_name=app_name,
        status_callback=status_callback,
    )
    status_callback(
        f"✅ Fallback: всего получено отзывов до фильтрации: {len(fallback_reviews)}"
    )

    all_reviews = rss_reviews + fallback_reviews

    if not all_reviews:
        raise RuntimeError(
            "Отзывы не получены. Возможные причины: нет публичных отзывов "
            "в российском App Store, регион ru недоступен для этого приложения, "
            "Apple ограничил доступ или изменил API."
        )

    status_callback(f"🔄 Объединено отзывов до удаления дублей: {len(all_reviews)}")

    unique_reviews = deduplicate_reviews(all_reviews)
    status_callback(f"🔄 После удаления дублей: {len(unique_reviews)}")

    recent_reviews = filter_reviews_last_year(unique_reviews)
    status_callback(f"🔄 После фильтрации за последние 365 дней: {len(recent_reviews)}")

    if not recent_reviews:
        raise RuntimeError("За последние 12 месяцев отзывы не найдены.")

    recent_reviews = sorted(
        recent_reviews,
        key=lambda x: x.get("_parsed_date") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    df = reviews_to_dataframe(recent_reviews)

    filename = f"appstore_reviews_{app_id}_{COUNTRY}_last_year.csv"

    return {
        "app_id": app_id,
        "app_name": app_name,
        "filename": filename,
        "reviews_count": len(recent_reviews),
        "dataframe": df,
        "csv_bytes": dataframe_to_csv_bytes(df),
    }


# -----------------------------
# Streamlit UI
# -----------------------------

st.set_page_config(
    page_title="App Store Reviews Scraper",
    page_icon="📱",
    layout="wide",
)

st.title("📱 App Store Reviews Scraper")
st.caption("Сбор отзывов из российского App Store за последние 12 месяцев")

with st.expander("ℹ️ Как работает скрипт", expanded=True):
    st.markdown(
        """
        Скрипт принимает ссылку на приложение в App Store, извлекает App Store ID,
        получает название приложения через Apple Lookup API и собирает отзывы из региона `ru`.

        Используются два источника:

        1. Официальный Apple RSS endpoint.
        2. Неофициальный HTTP fallback без Selenium, Playwright и браузерной автоматизации.

        ⚠️ App Store может ограничивать количество доступных отзывов.  
        ⚠️ Неофициальный fallback может перестать работать при изменении внутренних API Apple.
        """
    )

app_url = st.text_input(
    "Ссылка на приложение App Store",
    value="https://apps.apple.com/us/app/duolingo-language-lessons/id570060128",
    placeholder="https://apps.apple.com/us/app/duolingo-language-lessons/id570060128?l=ru",
)

start_button = st.button("Собрать отзывы", type="primary")

if start_button:
    log_box = st.empty()
    progress = st.progress(0)
    logs = []

    def streamlit_log(message):
        logs.append(message)
        log_box.code("\n".join(logs), language="text")

    try:
        progress.progress(5)

        with st.spinner("Собираю отзывы из App Store..."):
            result = run_scraper(app_url, status_callback=streamlit_log)

        progress.progress(100)

        st.success("Готово!")

        col1, col2, col3 = st.columns(3)

        with col1:
            st.metric("Приложение", result["app_name"])

        with col2:
            st.metric("App Store ID", result["app_id"])

        with col3:
            st.metric("Отзывов в CSV", result["reviews_count"])

        st.download_button(
            label="⬇️ Скачать CSV",
            data=result["csv_bytes"],
            file_name=result["filename"],
            mime="text/csv",
            type="primary",
        )

        st.subheader("Предпросмотр данных")
        st.dataframe(result["dataframe"], use_container_width=True)

    except ValueError as e:
        progress.empty()
        st.error(f"Неверная ссылка: {e}")

    except RuntimeError as e:
        progress.empty()
        st.error(str(e))

    except Exception as e:
        progress.empty()
        st.error(f"Непредвиденная ошибка: {e}")