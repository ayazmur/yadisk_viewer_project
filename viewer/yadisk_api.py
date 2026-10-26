import aiohttp
import logging
from urllib.parse import urlencode, urlparse, quote
import hashlib
import asyncio

from django.core.cache import cache
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

BASE_PUBLIC_API_URL = "https://cloud-api.yandex.net/v1/disk/public/resources"
CACHE_TTL = 300 

# --- Async Cache Helpers ---
sync_cache_get = sync_to_async(cache.get)
sync_cache_set = sync_to_async(cache.set)

# --- Utility Functions ---

def extract_public_key(url: str) -> str | None:
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        parsed_url = urlparse(url)

        if parsed_url.netloc in ('disk.yandex.ru', 'disk.yandex.com', 'yadi.sk'):
            path_parts = [part for part in parsed_url.path.split('/') if part]
            if len(path_parts) >= 1 and path_parts[0] in ('d', 'i', 'public'):
                 return url
            elif len(path_parts) == 1 and parsed_url.netloc == 'yadi.sk':
                 return url 

        logger.warning(f"Не удалось извлечь public_key из URL-адреса: {url}")
        return None
    except Exception as e:
        logger.error(f"Ошибка при извлечении публичного ключа из URL {url}: {e}")
        return None

def generate_cache_key(prefix: str, *args) -> str:
    key_string = f"{prefix}:{':'.join(map(str, args))}"
    return hashlib.md5(key_string.encode()).hexdigest()

# --- API Interaction Functions ---

async def get_public_resource_meta(
    public_key: str,
    path: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: aiohttp.ClientSession | None = None
) -> dict | None:
    current_path = path or '/'
    cache_key = generate_cache_key("yd_meta_v3", public_key, current_path, limit, offset)
    logger.debug(f"Проверка кэша по ключу: {cache_key} (URL={public_key}, path={current_path}, limit={limit}, offset={offset})")

    cached_data = await sync_cache_get(cache_key)
    if cached_data is not None:
        logger.info(f"Поиск ключа в кэше: {cache_key} (найдено)")
        return cached_data
    else:
        logger.info(f"Ошибка кэширования ключа (не найден): {cache_key}")

    params = {
        'public_key': public_key,
        'limit': limit,
        'offset': offset,
        'sort': 'type,name'
    }
    if path:
        params['path'] = path

    api_url = f"{BASE_PUBLIC_API_URL}?{urlencode(params, quote_via=quote)}"

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        async with session.get(api_url) as response:
            logger.debug(f"Запрашивающие метаданные: URL={api_url}, Статус={response.status}")

            if response.status == 200:
                data = await response.json()
                if isinstance(data, dict) and 'error' not in data:
                    logger.info(f"Настройка кэша для ключа: {cache_key} с TTL: {CACHE_TTL}")
                    await sync_cache_set(cache_key, data, timeout=CACHE_TTL)
                elif 'error' in data:
                     logger.warning(f"Получена ошибка API, не кэшируется ответ для ключа {cache_key}. Ошибка: {data.get('message')}")
                return data
            elif response.status == 404:
                logger.warning(f"Ресурс не найден (404) для URL: {api_url}")
                return {'error': 'not_found', 'message': 'Resource not found'}
            else:
                error_text = await response.text()
                logger.error(f"Ошибка API Яндекс.Диска ({response.status}) для URL {api_url}: {error_text}")
                return {'error': 'api_error', 'status': response.status, 'message': error_text}

    except aiohttp.ClientError as e:
        logger.error(f"Сетевая ошибка при запросе метаданных {public_key} (path={current_path}): {e}")
        return None
    except asyncio.TimeoutError:
        logger.error(f"Тайм-аут при запросе метаданных {public_key} (path={current_path})")
        return None
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка при запросе метаданных {public_key} (path={current_path}): {e}")
        return None
    finally:
        if close_session and session:
            await session.close()


async def get_public_resource_download_link(
    public_key: str,
    path: str | None = None,
    session: aiohttp.ClientSession | None = None
) -> str | None:
    params = {'public_key': public_key}
    if path:
        params['path'] = path

    api_url = f"{BASE_PUBLIC_API_URL}/download?{urlencode(params, quote_via=quote)}"

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        async with session.get(api_url) as response:
            logger.debug(f"Запрашиваю ссылку для скачивания: URL={api_url}, Статус={response.status}")
            if response.status == 200:
                data = await response.json()
                download_url = data.get('href')
                if not download_url:
                     logger.warning(f"API Яндекса вернул 200 OK, но ссылка ('href') отсутствует для {api_url}")
                     return None
                return download_url
            else:
                error_text = await response.text()
                logger.error(f"Ошибка при получении ссылки из API Яндекса ({response.status}) для {api_url}: {error_text}")
                return None

    except aiohttp.ClientError as e:
        logger.error(f"Сетевая ошибка при запросе ссылки для скачивания {public_key} (path={path}): {e}")
        return None
    except asyncio.TimeoutError:
         logger.error(f"Тайм-аут при запросе ссылки для скачивания {public_key} (path={path})")
         return None
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка при запросе ссылки для скачивания {public_key} (path={path}): {e}")
        return None
    finally:
        if close_session and session:
            await session.close()


async def get_all_public_files(
    public_key: str,
    base_path: str = '/',
    session: aiohttp.ClientSession | None = None
) -> list[dict[str, str]]:
    """Рекурсивно собирает все файлы из публичного диска по указанному пути."""
    all_files = []
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        await _scan_directory(public_key, base_path, session, all_files)
    except Exception as e:
        logger.error(f"Ошибка рекурсивного обхода диска {public_key} (path={base_path}): {e}")
    finally:
        if close_session and session:
            await session.close()

    logger.info(f"Рекурсивный обход завершён: найдено {len(all_files)} файлов.")
    return all_files


async def _scan_directory(
    public_key: str,
    path: str,
    session: aiohttp.ClientSession,
    all_files: list[dict[str, str]]
) -> None:
    """Сканирует одну директорию и рекурсивно обрабатывает подпапки."""
    offset = 0
    limit = 50

    while True:
        data = await get_public_resource_meta(public_key, path=path, limit=limit, offset=offset, session=session)
        if not data or 'embedded' not in data or 'items' not in data['embedded']:
            break

        items = data['embedded']['items']
        dirs = [i for i in items if i.get('type') == 'dir']
        files = [i for i in items if i.get('type') == 'file']

        for file_item in files:
            api_path = file_item.get('path', '/')
            logger.debug(f"Найден файл: {api_path}")

            download_url = await get_public_resource_download_link(public_key, path=api_path, session=session)
            if download_url:
                all_files.append({'api_path': api_path, 'url': download_url})
            else:
                logger.warning(f"Не удалось получить ссылку для файла: {api_path}")

        for dir_item in dirs:
            dir_path = dir_item.get('path', '/')
            logger.debug(f"Рекурсивный обход подпапки: {dir_path}")
            await _scan_directory(public_key, dir_path, session, all_files)

        offset += len(items)
        if len(items) < limit:
            break


# --- Own Disk (OAuth) API ---
DISK_RESOURCES_API_URL = "https://cloud-api.yandex.net/v1/disk/resources"
DISK_DOWNLOAD_API_URL = "https://cloud-api.yandex.net/v1/disk/resources/download"


def _disk_headers(token: str) -> dict[str, str]:
    return {'Authorization': f'OAuth {token}'}


def normalize_disk_path(path: str | None) -> str:
    """Приводит путь к виду '/folder/file' (корень = '/')."""
    if not path:
        return '/'
    p = path.replace('\\', '/')
    if p.startswith('disk:'):
        p = p[5:]
    p = '/' + p.lstrip('/')
    return p if p else '/'


async def get_disk_resource_meta(
    token: str,
    path: str = '/',
    limit: int = 50,
    offset: int = 0,
    session: aiohttp.ClientSession | None = None
) -> dict | None:
    """Метаданные ресурса собственного диска пользователя (OAuth-токен)."""
    params = {
        'path': normalize_disk_path(path),
        'limit': limit,
        'offset': offset,
        'sort': 'type,name',
    }

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        async with session.get(DISK_RESOURCES_API_URL, params=params, headers=_disk_headers(token)) as response:
            if response.status == 200:
                return await response.json()
            if response.status == 404:
                return {'error': 'not_found', 'message': 'Resource not found'}
            if response.status in (401, 403):
                return {'error': 'unauthorized', 'status': response.status, 'message': await response.text()}
            error_text = await response.text()
            logger.error(f"Ошибка API Яндекс.Диска ({response.status}) для path={params['path']}: {error_text}")
            return {'error': 'api_error', 'status': response.status, 'message': error_text}
    except aiohttp.ClientError as e:
        logger.error(f"Сетевая ошибка при запросе ресурса диска (path={params['path']}): {e}")
        return None
    except asyncio.TimeoutError:
        logger.error(f"Тайм-аут при запросе ресурса диска (path={params['path']})")
        return None
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка при запросе ресурса диска (path={params['path']}): {e}")
        return None
    finally:
        if close_session and session:
            await session.close()


async def get_disk_download_link(
    token: str,
    path: str,
    session: aiohttp.ClientSession | None = None
) -> str | None:
    """Ссылка на скачивание файла с собственного диска пользователя."""
    params = {'path': normalize_disk_path(path)}

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        async with session.get(DISK_DOWNLOAD_API_URL, params=params, headers=_disk_headers(token)) as response:
            if response.status == 200:
                data = await response.json()
                return data.get('href')
            error_text = await response.text()
            logger.warning(f"Не удалось получить ссылку (path={params['path']}, status={response.status}): {error_text}")
            return None
    except Exception as e:
        logger.error(f"Ошибка получения ссылки на скачивание (path={params['path']}): {e}")
        return None
    finally:
        if close_session and session:
            await session.close()


async def get_all_disk_files(
    token: str,
    base_path: str = '/',
    session: aiohttp.ClientSession | None = None
) -> list[dict[str, str]]:
    """Рекурсивно собирает все файлы собственного диска пользователя."""
    all_files: list[dict[str, str]] = []
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        await _scan_disk_directory(token, normalize_disk_path(base_path), session, all_files)
    except Exception as e:
        logger.error(f"Ошибка рекурсивного обхода диска (path={base_path}): {e}")
    finally:
        if close_session and session:
            await session.close()

    logger.info(f"Рекурсивный обход своего диска завершён: найдено {len(all_files)} файлов.")
    return all_files


async def _scan_disk_directory(
    token: str,
    path: str,
    session: aiohttp.ClientSession,
    all_files: list[dict[str, str]]
) -> None:
    offset = 0
    limit = 50

    while True:
        data = await get_disk_resource_meta(token, path=path, limit=limit, offset=offset, session=session)
        if not data or 'error' in data:
            break

        embedded = data.get('_embedded') or data.get('embedded') or {}
        items = embedded.get('items', []) or []

        for file_item in (i for i in items if i.get('type') == 'file'):
            file_path = file_item.get('path', '')
            download_url = await get_disk_download_link(token, file_path, session=session)
            if download_url:
                all_files.append({'api_path': file_path, 'url': download_url})
            else:
                logger.warning(f"Не удалось получить ссылку для файла: {file_path}")

        for dir_item in (i for i in items if i.get('type') == 'dir'):
            await _scan_disk_directory(token, dir_item.get('path', '/'), session, all_files)

        offset += len(items)
        if len(items) < limit:
            break
