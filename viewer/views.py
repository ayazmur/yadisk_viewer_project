import aiohttp, asyncio, logging, mimetypes, zipfile, os, tempfile, aiofiles, re
import json, shutil, time, uuid
import requests
from urllib.parse import quote, unquote, urlencode
from pathlib import PurePath, Path
from typing import AsyncGenerator, List, Dict, Any, Set, Tuple
import math

# Django Imports
from django.shortcuts import render, redirect
from django.http import (
    HttpRequest, HttpResponse, StreamingHttpResponse, FileResponse,
    Http404, HttpResponseRedirect, HttpResponseServerError, JsonResponse
)
from django.views.decorators.http import require_GET, require_POST
from django.utils.text import slugify
from django.utils.timezone import now
from django.conf import settings
from django.contrib.auth import logout
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.urls import reverse

# Local Imports
from .yadisk_api import (
    get_public_resource_meta, get_public_resource_download_link,
    extract_public_key, get_all_public_files,
    get_disk_resource_meta, get_disk_download_link, get_all_disk_files,
    normalize_disk_path,
    create_disk_folder_sync, get_disk_upload_link_sync,
)

# Async Helpers
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# --- Constants ---
DEFAULT_PAGE_SIZE = 50
ALLOWED_PAGE_SIZES = [25, 50, 100]
DOWNLOAD_CONCURRENCY_LIMIT = 5 
FILE_CHUNK_SIZE = 8192
MAX_ZIP_DOWNLOAD_SIZE_MB = 2048
RECURSIVE_DOWNLOAD_CONCURRENCY_LIMIT = 5
RECURSIVE_DOWNLOAD_TIMEOUT = 3600
INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_RESOURCE_NAME_LENGTH = 255

UPLOAD_SESSION_ROOT = os.path.join(tempfile.gettempdir(), 'yadisk_uploads')
UPLOAD_SESSION_TTL = 6 * 3600
UPLOAD_CHUNK_DEFAULT_BYTES = 4 * 1024 * 1024
UPLOAD_CHUNK_MIN_BYTES = 256 * 1024
UPLOAD_CHUNK_MAX_BYTES = 16 * 1024 * 1024
UPLOAD_ID_RE = re.compile(r'^[0-9a-f]{32}$')

FILE_EXTENSION_ICONS = {    'pdf': 'pdf',
    'doc': 'doc',
    'docx': 'doc',
    'xls': 'xls',
    'xlsx': 'xls',
    'ppt': 'ppt',
    'pptx': 'ppt',
    'txt': 'txt',
    'md': 'md',
    'py': 'py',
    'js': 'js',
    'html': 'html',
    'htm': 'html',
    'css': 'html',
    'json': 'json',
    'csv': 'csv',
    'zip': 'zip',
    'rar': 'zip',
    '7z': 'zip',
    'tar': 'zip',
    'gz': 'zip',
    'mp3': 'mp3',
    'wav': 'mp3',
    'ogg': 'mp3',
    'mp4': 'mp4',
    'avi': 'mp4',
    'mov': 'mp4',
    'mkv': 'mp4',
    'webm': 'mp4',
}

INLINE_FALLBACK_MIME = {
    '.webp': 'image/webp',
    '.heic': 'image/heic',
    '.avif': 'image/avif',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.m4v': 'video/mp4',
    '.webm': 'video/webm',
    '.flac': 'audio/flac',
    '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg',
    '.wav': 'audio/wav',
    '.m4a': 'audio/mp4',
}

INLINE_STREAM_CHUNK_SIZE = 64 * 1024


def guess_inline_mime(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename or '')
    if mime_type:
        return mime_type
    return INLINE_FALLBACK_MIME.get(PurePath(filename or '').suffix.lower(), 'application/octet-stream')


def get_extension_icon(filename: str) -> str:
    ext = PurePath(filename).suffix.lstrip('.').lower()
    if not ext:
        return "file.svg"
    icon_name = FILE_EXTENSION_ICONS.get(ext)
    if icon_name:
        return f"viewer/icons/{icon_name}.svg"
    return "file.svg"


# --- Helper Functions ---

def get_file_category(item: Dict[str, Any]) -> str:
    if item.get('type') != 'file':
        return 'dir' 

    mime_type, _ = mimetypes.guess_type(item.get('name', ''))
    if not mime_type:
        return 'other'

    major_type = mime_type.split('/')[0]
    if major_type == 'image': return 'image'
    if major_type == 'video': return 'video'
    if major_type == 'audio': return 'audio'

    # Broaden document category
    if major_type == 'text' or mime_type in (
        'application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-powerpoint',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/rtf', 'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation'
    ):
        return 'document'

    # Archive types
    if mime_type in (
        'application/zip', 'application/x-rar-compressed', 'application/vnd.rar',
        'application/x-7z-compressed', 'application/gzip', 'application/x-tar',
        'application/x-bzip2', 'application/x-xz'
    ):
        return 'archive'

    return 'other'

def filter_items(items: List[Dict[str, Any]], filter_type: str) -> List[Dict[str, Any]]:
    if filter_type == 'all':
        return items
    return [
        item for item in items
        if item.get('type') == 'dir' or get_file_category(item) == filter_type
    ]

def generate_breadcrumbs(path_str: str) -> List[Dict[str, str]]:
    breadcrumbs = [{'name': 'Корень', 'path': '/'}]
    normalized_path = Path(path_str.replace('\\', '/')).as_posix().strip('/')
    parts = [part for part in normalized_path.split('/') if part]
    current_crumb_path = ''
    for part in parts:
        current_crumb_path += f'/{part}'
        breadcrumbs.append({'name': part, 'path': current_crumb_path})
    return breadcrumbs

def process_api_items(items_list_raw: List[Dict[str, Any]], relative_to_disk_root: bool = False) -> Tuple[List[Dict[str, Any]], Set[str]]:
    processed_items = []
    available_types = set()
    for item in items_list_raw:
        item_copy = item.copy()
        item_type = item_copy.get('type')

        if item_type == 'file':
            category = get_file_category(item_copy)
            available_types.add(category)
            api_path_str = item_copy.get('path', '')

            if api_path_str.startswith('disk:'):
                if relative_to_disk_root:
                    relative_path = api_path_str[5:].lstrip('/')
                else:
                    public_root_prefix_len = api_path_str.find('/', 6) + 1
                    relative_path = api_path_str[public_root_prefix_len:] if public_root_prefix_len > 0 else ''
            else:
                 relative_path = api_path_str

            item_copy['url_path'] = Path(relative_path.replace('\\', '/')).as_posix()
            item_copy['normalized_api_path'] = f"/{item_copy['url_path'].lstrip('/')}"
            item_copy['icon_path'] = get_extension_icon(item_copy.get('name', ''))

        elif item_type == 'dir':
            dir_path = item_copy.get('path', '/')
            if dir_path.startswith('disk:'):
                if relative_to_disk_root:
                    relative_path = dir_path[5:].lstrip('/')
                    item_copy['path_param'] = f"/{relative_path}".replace('//', '/')
                else:
                    public_root_prefix_len = dir_path.find('/', 6) + 1
                    relative_path = dir_path[public_root_prefix_len:] if public_root_prefix_len > 0 else ''
                    item_copy['path_param'] = f"/{relative_path}".replace('//','/')
            else:
                item_copy['path_param'] = f"/{dir_path.lstrip('/')}".replace('//','/')

        processed_items.append(item_copy)

    logger.debug(f"process_api_items обнаружил типы файлов: {available_types}")
    return processed_items, available_types

# --- Async Session Helpers ---
async def get_session_value(request: HttpRequest, key: str, default: Any = None):
    return await sync_to_async(request.session.get)(key, default)

async def set_session_value(request: HttpRequest, key: str, value: Any):
    await sync_to_async(request.session.__setitem__)(key, value)
    await sync_to_async(request.session.save)()

async def pop_session_value(request: HttpRequest, key: str, default: Any = None):
    val = await sync_to_async(request.session.pop)(key, default)
    await sync_to_async(request.session.save)()
    return val

async def clear_session_keys(request: HttpRequest, keys: List[str]):
    changed = False
    for key in keys:
        if key in request.session:
            await pop_session_value(request, key)
            changed = True
    if changed:
        await sync_to_async(request.session.save)()


def _get_yandex_token_sync(request: HttpRequest) -> str | None:
    try:
        user = request.user
        if not user.is_authenticated:
            return None
        social = user.social_auth.filter(provider='yandex-oauth2').first()
        if not social:
            return None
        return (social.extra_data or {}).get('access_token')
    except Exception as e:
        logger.warning(f"Не удалось получить OAuth-токен Яндекса: {e}")
        return None


async def get_yandex_token(request: HttpRequest) -> str | None:
    return await sync_to_async(_get_yandex_token_sync)(request)


def _wants_json(request: HttpRequest) -> bool:
    return (
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('accept', '')
    )


def _json_error(message: str, code: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'ok': False, 'error': code, 'message': message}, status=status)


def validate_resource_name(name: str) -> str | None:
    """Возвращает текст ошибки для недопустимого имени или None."""
    if not name:
        return 'Имя не может быть пустым.'
    if name in ('.', '..'):
        return 'Недопустимое имя.'
    if len(name) > MAX_RESOURCE_NAME_LENGTH:
        return f'Имя слишком длинное (максимум {MAX_RESOURCE_NAME_LENGTH} символов).'
    if INVALID_NAME_CHARS.search(name):
        return 'Имя не должно содержать символы \\ / : * ? " < > |.'
    return None


# --- Chunked upload helpers ---

UPLOAD_LINK_ERRORS = {
    'noperm': ('Нет права записи. Добавьте в приложении Яндекса доступ «Яндекс.Диск REST API» с записью и войдите заново.', 403),
    'exists': ('Файл с таким именем уже существует.', 409),
    'network': ('Не удалось связаться с Яндекс.Диском.', 502),
    'error': ('Не удалось подготовить загрузку файла.', 502),
}


def _disk_write_token(request: HttpRequest) -> Tuple[str | None, str | None]:
    if request.session.get('view_mode', 'public') != 'disk':
        return None, 'public_mode'
    token = _get_yandex_token_sync(request)
    if not token:
        return None, 'nologin'
    return token, None


def _json_upload_auth_error(code: str) -> JsonResponse:
    if code == 'nologin':
        return _json_error('Сессия авторизации истекла — войдите через Яндекс заново.', code, 401)
    return _json_error('Загрузка доступна только в режиме «Мой диск».', code, 403)


def _upload_session_dir(upload_id: str) -> str | None:
    if not upload_id or not UPLOAD_ID_RE.fullmatch(upload_id):
        return None
    return os.path.join(UPLOAD_SESSION_ROOT, upload_id)


def _read_upload_meta(upload_id: str) -> dict | None:
    directory = _upload_session_dir(upload_id)
    if not directory:
        return None
    try:
        with open(os.path.join(directory, 'meta.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_upload_meta(upload_id: str, meta: dict) -> None:
    directory = _upload_session_dir(upload_id)
    if not directory:
        return
    with open(os.path.join(directory, 'meta.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh)


def _discard_upload(upload_id: str) -> None:
    directory = _upload_session_dir(upload_id)
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def _cleanup_stale_uploads() -> None:
    try:
        entries = list(os.scandir(UPLOAD_SESSION_ROOT))
    except OSError:
        return
    now_ts = time.time()
    for entry in entries:
        try:
            if entry.is_dir() and now_ts - entry.stat().st_mtime > UPLOAD_SESSION_TTL:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


# --- Main View ---

async def index(request: HttpRequest) -> HttpResponse:
    context = {
        'error': None,
        'data': None,
        'items': None,
        'public_url': '',
        'filter_type': 'all',
        'resource_type': None,
        'available_file_types': set(),
        'current_path': '/',
        'breadcrumbs': [],
        'pagination': None,
        'current_page_size': DEFAULT_PAGE_SIZE,
        'allowed_page_sizes': ALLOWED_PAGE_SIZES,
        'sort_by': 'name',
        'sort_order': 'asc',
        'parent_path': None,
        'total_items': 0,
        'dir_count': 0,
        'file_count': 0,
        'file_extension_icons': FILE_EXTENSION_ICONS,
        'disk_mode': False,
        'has_disk_access': False,
        'can_write': False,
        'view_mode': 'public',
    }

    # Get parameters from GET request
    public_url_from_query = request.GET.get('public_url', '').strip()
    current_path_param = request.GET.get('path', '/')
    filter_from_get = request.GET.get('filter', 'all')
    try:
        page_num = int(request.GET.get('page', '1'))
        page_num = max(1, page_num)
    except ValueError:
        page_num = 1
    try:
        page_size = int(request.GET.get('limit', str(DEFAULT_PAGE_SIZE)))
        page_size = page_size if page_size in ALLOWED_PAGE_SIZES else DEFAULT_PAGE_SIZE
    except ValueError:
        page_size = DEFAULT_PAGE_SIZE

    sort_by = request.GET.get('sort', 'name')
    if sort_by not in ('name', 'size'):
        sort_by = 'name'
    sort_order = request.GET.get('order', 'asc')
    if sort_order not in ('asc', 'desc'):
        sort_order = 'asc'
    context['sort_by'] = sort_by
    context['sort_order'] = sort_order

    public_url_from_post = request.POST.get('public_url', '').strip() if request.method == 'POST' else ''
    session_public_key = await get_session_value(request, 'public_key')
    force_disk = request.GET.get('source') == 'disk'
    force_public = request.GET.get('source') == 'public'

    token = await get_yandex_token(request)

    public_url = public_url_from_post or public_url_from_query
    if not public_url and not force_disk and not force_public:
        public_url = session_public_key

    disk_mode = bool(token) and not public_url and not force_public
    context['disk_mode'] = disk_mode
    context['has_disk_access'] = bool(token)
    context['can_write'] = disk_mode and bool(token)
    context['view_mode'] = 'disk' if disk_mode else 'public'

    context['public_url'] = public_url or ''
    context['current_page_size'] = page_size
    context['filter_type'] = filter_from_get
    context['upload_status'] = request.GET.get('upload')

    if force_disk:
        await clear_session_keys(request, ['public_key', 'resource_type', 'resource_name'])

    if force_public:
        await clear_session_keys(request, ['public_key', 'resource_type', 'resource_name'])

    if not public_url and not disk_mode:
        logger.info("Нет public_url и нет авторизации. Очистка сессии.")
        await clear_session_keys(request, ['public_key', 'resource_type', 'resource_name'])
        return await sync_to_async(render, thread_sensitive=True)(request, 'viewer/index.html', context)

    # --- Process Public URL ---
    public_key = extract_public_key(public_url) if public_url else None
    if public_url and not public_key:
        context['error'] = "Некорректный URL или не удалось извлечь ключ."
        await clear_session_keys(request, ['public_key', 'resource_type', 'resource_name'])
        return await sync_to_async(render, thread_sensitive=True)(request, 'viewer/index.html', context)

    if public_key and (public_key != session_public_key or public_url_from_post):
        await set_session_value(request, 'public_key', public_key)
        await clear_session_keys(request, ['resource_type', 'resource_name'])
        logger.info(f"Установлен/Обновлен public_key в сессии: {public_key}")
        if request.method == 'POST':
             query_params = request.GET.copy()
             query_params['public_url'] = public_key
             query_params['path'] = '/'
             return redirect(f"{request.path}?{query_params.urlencode()}")

    await set_session_value(request, 'view_mode', 'disk' if disk_mode else 'public')


    # --- Fetch and Process Data ---
    normalized_path_param = Path(current_path_param.replace('\\', '/')).as_posix()
    api_path = normalized_path_param if normalized_path_param != '/' else None
    offset = (page_num - 1) * page_size
    context['current_path'] = normalized_path_param
    context['breadcrumbs'] = generate_breadcrumbs(normalized_path_param)

    if normalized_path_param != '/':
        path_parts = [part for part in normalized_path_param.split('/') if part]
        context['parent_path'] = '/' + '/'.join(path_parts[:-1]) if len(path_parts) > 1 else '/'

    logger.info(f"Запрос данных: mode={'disk' if disk_mode else 'public'}, key='{public_key}', path='{normalized_path_param}', limit={page_size}, offset={offset}")

    await set_session_value(request, 'current_path', normalized_path_param)

    async with aiohttp.ClientSession() as session:
        if disk_mode:
            meta_data = await get_disk_resource_meta(
                token, path=normalized_path_param, limit=page_size, offset=offset, session=session
            )
        else:
            meta_data = await get_public_resource_meta(
                public_key, path=api_path, limit=page_size, offset=offset, session=session
            )

    # --- Handle API Response ---
    if meta_data is None:
        context['error'] = "Ошибка сети при получении данных от API Яндекс.Диска."
    elif meta_data.get('error') == 'not_found':
        context['error'] = f"Ресурс не найден по пути: {normalized_path_param}"
    elif meta_data.get('error') == 'unauthorized':
        context['error'] = "Нет доступа к вашему диску. Войдите через Яндекс заново."
    elif 'error' in meta_data:
        context['error'] = f"Ошибка API Яндекс.Диска: {meta_data.get('message', 'Неизвестная ошибка')}"
        if not disk_mode:
            await clear_session_keys(request, ['public_key', 'resource_type', 'resource_name'])
    else:
        # --- Process Successful Response ---
        resource_type = meta_data.get('type') 
        resource_name = meta_data.get('name', 'yadisk-item')

        session_resource_type = await get_session_value(request, 'resource_type')
        if not session_resource_type:
             if disk_mode:
                 await set_session_value(request, 'resource_type', resource_type)
                 await set_session_value(request, 'resource_name', resource_name or 'Мой диск')
                 logger.info(f"Режим своего диска: тип ({resource_type}) сохранён в сессии.")
             else:
                 async with aiohttp.ClientSession() as session:
                     root_meta = await get_public_resource_meta(public_key, path=None, limit=1, offset=0, session=session)
                     if root_meta and 'error' not in root_meta:
                         base_type = root_meta.get('type')
                         base_name = root_meta.get('name', 'yadisk-archive')
                         await set_session_value(request, 'resource_type', base_type)
                         await set_session_value(request, 'resource_name', base_name)
                         logger.info(f"Тип корневого ресурса ({base_type}) и имя ({base_name}) сохранены в сессии.")
                     else:
                          logger.warning("Не удалось получить метаданные корневого ресурса для сохранения в сессии.")


        context['data'] = meta_data

        items_list_processed = []
        total_items = 0

        if resource_type == 'file':
            processed_single, available_types_single = process_api_items([meta_data], relative_to_disk_root=disk_mode)
            if processed_single:
                context['data'] = processed_single[0]
                items_list_processed = processed_single
            total_items = 1
            context['available_file_types'] = available_types_single
            context['items'] = items_list_processed

        elif resource_type == 'dir' and '_embedded' in meta_data:
            raw_items = meta_data['_embedded']['items']
            all_items_processed, available_types = process_api_items(raw_items, relative_to_disk_root=disk_mode)
            context['available_file_types'] = available_types
            total_items = meta_data['_embedded'].get('total', len(all_items_processed))

            context['items'] = filter_items(all_items_processed, filter_from_get) if filter_from_get != 'all' else all_items_processed
        else:
            context['items'] = []
            context['available_file_types'] = set()
            total_items = 0

        context['total_items'] = total_items

        if resource_type == 'dir':
            current_items = context.get('items') or []
            context['dir_count'] = sum(1 for item in current_items if item.get('type') == 'dir')
            context['file_count'] = sum(1 for item in current_items if item.get('type') == 'file')

            if sort_by != 'name' or sort_order != 'asc':
                dirs = [item for item in current_items if item.get('type') == 'dir']
                files = [item for item in current_items if item.get('type') == 'file']
                dirs.sort(key=lambda item: (item.get('name', '') or '').lower())
                files.sort(
                    key=lambda item: item.get('size', 0) if sort_by == 'size' else (item.get('name', '') or '').lower(),
                    reverse=(sort_order == 'desc')
                )
                context['items'] = dirs + files

        # --- Setup Pagination ---
        if resource_type == 'dir' and total_items > page_size:
            total_pages = math.ceil(total_items / page_size)
            if total_pages > 1:
                 context['pagination'] = {
                    'current_page': page_num,
                    'total_pages': total_pages,
                    'total_items': total_items,
                    'start_index': min(total_items, (page_num - 1) * page_size + 1),
                    'end_index': min(total_items, page_num * page_size),
                    'has_previous': page_num > 1,
                    'has_next': page_num < total_pages,
                    'page_range': range(max(1, page_num - 3), min(total_pages + 1, page_num + 4)),
                    'needs_first_ellipses': page_num > 4,
                    'needs_last_ellipses': page_num < total_pages - 3,
                }

    return await sync_to_async(render, thread_sensitive=True)(request, 'viewer/index.html', context)


# --- Download Views ---

@require_GET
async def download_file(request: HttpRequest, file_path: str) -> HttpResponse:
    logger.info(f"Запрос на скачивание одного файла: '{file_path}'")

    public_key = await get_session_value(request, 'public_key')
    base_resource_type = await get_session_value(request, 'resource_type')
    view_mode = await get_session_value(request, 'view_mode', 'public')
    disk_mode = view_mode == 'disk'

    token = await get_yandex_token(request) if disk_mode else None

    if disk_mode and not token:
        logger.warning("Попытка скачать файл со своего диска без OAuth-токена.")
        raise Http404("Сессия авторизации истекла. Войдите через Яндекс заново.")

    if not disk_mode and not public_key:
        logger.warning("Попытка скачать файл без public_key в сессии.")
        raise Http404("Сессия истекла или не найдена. Пожалуйста, введите URL заново.")

    try:
        clean_path_str = Path(unquote(file_path).replace('\\', '/')).as_posix().lstrip('/')
        logger.debug(f"Очищенный путь файла для скачивания: '{clean_path_str}'")
    except Exception as e:
        logger.error(f"Ошибка декодирования/очистки file_path: '{file_path}'. Ошибка: {e}")
        raise Http404("Некорректный путь файла в URL.")

    file_name = PurePath(clean_path_str).name or "downloaded_file"

    # 1. Получаем ссылку в отдельной короткоживущей сессии
    async with aiohttp.ClientSession() as session:
        if disk_mode:
            path_to_request = normalize_disk_path(clean_path_str)
            logger.debug(f"Запрос ссылки со своего диска: path='{path_to_request}'")
            download_url = await get_disk_download_link(token, path_to_request, session=session)
        else:
            path_to_request = f"/{clean_path_str}" if base_resource_type == 'dir' and clean_path_str else None
            logger.debug(f"Запрос ссылки: key={public_key}, path='{path_to_request}'")
            download_url = await get_public_resource_download_link(public_key, path=path_to_request, session=session)

    if not download_url:
        logger.error(f"Не удалось получить ссылку для '{clean_path_str}' (mode={'disk' if disk_mode else 'public'}).")
        raise Http404(f"Не удалось получить ссылку для скачивания файла '{file_name}'.")

    logger.info(f"Получена ссылка для '{file_name}', начинаю стриминг через сервер.")

    # 2. Асинхронный генератор с автоматическим повтором при разрыве ДО первого чанка
    async def stream_from_yadisk():
        timeout = aiohttp.ClientTimeout(total=3600, connect=60, sock_read=3600)
        headers = {'User-Agent': 'YadiskViewerApp/1.0 (+https://github.com/ayazmur/yadisk_viewer_project)'}

        sent_any = False  # отправили ли уже хоть один чанк браузеру

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as stream_session:
                    async with stream_session.get(download_url, timeout=timeout, headers=headers, allow_redirects=True) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.content.iter_chunked(FILE_CHUNK_SIZE):
                            sent_any = True
                            yield chunk
                return  # успешно завершили
            except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError, RuntimeError) as e:
                if sent_any:
                    logger.error(f"Разрыв после начала передачи, повтор невозможен: {e}")
                    return
                logger.warning(f"Разрыв до начала передачи, попытка {attempt + 1}/3: {e}")
                if attempt == 2:
                    logger.error(f"Все 3 попытки провалились для '{file_name}'.")
                    return
                await asyncio.sleep(1)
            except aiohttp.ClientResponseError as e:
                logger.error(f"Ошибка HTTP {e.status} при стриминге '{file_name}': {e.message}")
                return
            except asyncio.TimeoutError:
                logger.error(f"Тайм-аут при стриминге '{file_name}'.")
                return
            except Exception as e:
                logger.error(f"Ошибка стриминга '{file_name}': {e}")
                return

    response = StreamingHttpResponse(
        stream_from_yadisk(),
        content_type='application/octet-stream',
    )

    try:
        ascii_name = file_name.encode('ascii').decode('ascii')
        response['Content-Disposition'] = f'attachment; filename="{ascii_name}"'
    except UnicodeEncodeError:
        encoded_name = quote(file_name)
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_name}"

    logger.info(f"Стриминг файла '{file_name}' через сервер клиенту.")
    return response

@require_GET
async def preview_file(request: HttpRequest, file_path: str) -> HttpResponse:
    """Отдаёт файл inline с поддержкой Range для просмотра фото/видео в браузере."""
    public_key = await get_session_value(request, 'public_key')
    base_resource_type = await get_session_value(request, 'resource_type')
    view_mode = await get_session_value(request, 'view_mode', 'public')
    disk_mode = view_mode == 'disk'
    token = await get_yandex_token(request) if disk_mode else None

    if disk_mode and not token:
        raise Http404("Сессия авторизации истекла. Войдите через Яндекс заново.")
    if not disk_mode and not public_key:
        raise Http404("Сессия истекла или не найдена. Введите ссылку заново.")

    try:
        clean_path_str = Path(unquote(file_path).replace('\\', '/')).as_posix().lstrip('/')
    except Exception as e:
        logger.error(f"Ошибка декодирования пути для просмотра '{file_path}': {e}")
        raise Http404("Некорректный путь файла.")

    file_name = PurePath(clean_path_str).name or 'file'
    content_type = guess_inline_mime(file_name)

    async with aiohttp.ClientSession() as link_session:
        if disk_mode:
            download_url = await get_disk_download_link(
                token, normalize_disk_path(clean_path_str), session=link_session
            )
        else:
            path_to_request = f"/{clean_path_str}" if base_resource_type == 'dir' and clean_path_str else None
            download_url = await get_public_resource_download_link(
                public_key, path=path_to_request, session=link_session
            )

    if not download_url:
        logger.warning(f"Не удалось получить ссылку для просмотра '{clean_path_str}'.")
        raise Http404(f"Не удалось получить ссылку для файла '{file_name}'.")

    upstream_headers = {
        'User-Agent': 'YadiskViewerApp/1.0 (+https://github.com/ayazmur/yadisk_viewer_project)',
    }
    range_header = request.headers.get('Range')
    if range_header:
        upstream_headers['Range'] = range_header

    media_session = aiohttp.ClientSession()
    try:
        upstream = await media_session.get(
            download_url,
            headers=upstream_headers,
            timeout=aiohttp.ClientTimeout(total=None, connect=60, sock_read=3600),
            allow_redirects=True,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        await media_session.close()
        logger.warning(f"Не удалось открыть поток '{file_name}': {e}")
        raise Http404("Не удалось открыть файл для просмотра.")

    if upstream.status == 416:
        content_range = upstream.headers.get('Content-Range', '')
        upstream.release()
        await media_session.close()
        response = HttpResponse(status=416)
        if content_range:
            response['Content-Range'] = content_range
        return response

    if upstream.status not in (200, 206):
        logger.error(f"Ошибка потока '{file_name}': HTTP {upstream.status}")
        upstream.release()
        await media_session.close()
        raise Http404("Не удалось открыть файл для просмотра.")

    async def stream_body():
        try:
            async for chunk in upstream.content.iter_chunked(INLINE_STREAM_CHUNK_SIZE):
                yield chunk
        except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError) as e:
            logger.warning(f"Поток '{file_name}' прерван: {e}")
        finally:
            upstream.release()
            await media_session.close()

    response = StreamingHttpResponse(
        stream_body(),
        status=206 if upstream.status == 206 else 200,
        content_type=content_type,
    )
    response['Accept-Ranges'] = 'bytes'
    response['Cache-Control'] = 'private, max-age=600'
    if upstream.headers.get('Content-Range'):
        response['Content-Range'] = upstream.headers['Content-Range']
    if upstream.headers.get('Content-Length'):
        response['Content-Length'] = upstream.headers['Content-Length']

    try:
        ascii_name = file_name.encode('ascii').decode('ascii')
        response['Content-Disposition'] = f'inline; filename="{ascii_name}"'
    except UnicodeEncodeError:
        response['Content-Disposition'] = f"inline; filename*=UTF-8''{quote(file_name)}"

    logger.info(f"Inline-просмотр '{file_name}' ({content_type}, range={bool(range_header)}).")
    return response


async def _fetch_download_links(public_key: str, api_paths: List[str], session: aiohttp.ClientSession, token: str | None = None) -> Tuple[List[Dict[str, str]], List[str]]:
    link_tasks = []
    for api_path in api_paths:
        if token:
            link_tasks.append(
                get_disk_download_link(token, normalize_disk_path(api_path), session=session)
            )
        else:
            path_param = api_path if api_path != '/' else None
            link_tasks.append(
                get_public_resource_download_link(public_key, path=path_param, session=session)
            )

    logger.info(f"Запрос {len(link_tasks)} ссылок на скачивание...")
    download_links_results = await asyncio.gather(*link_tasks, return_exceptions=True)

    files_to_download = []
    failed_links = []
    for i, result in enumerate(download_links_results):
        api_path = api_paths[i]
        if isinstance(result, Exception) or result is None:
            logger.warning(f"Не удалось получить ссылку для '{api_path}': {result}")
            failed_links.append(api_path)
        else:
            files_to_download.append({'api_path': api_path, 'url': result})

    return files_to_download, failed_links

async def _download_file_to_temp(file_info: Dict[str, str], temp_dir: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> Tuple[str, str] | None:
    async with semaphore:
        api_path = file_info['api_path']
        url = file_info['url']
        original_filename = PurePath(api_path.lstrip('/')).name or f"file_{hash(api_path)}"
        temp_file_path = os.path.join(temp_dir, f"{original_filename}.{abs(hash(api_path))}.tmp")
        logger.debug(f"Начало скачивания '{api_path}' в '{temp_file_path}'")

        try:
            timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=600)
            headers = {'User-Agent': 'YadiskViewerApp/1.0 (+https://github.com/ayazmur/yadisk_viewer_project)'}

            async with session.get(url, timeout=timeout, headers=headers, allow_redirects=True) as resp:
                resp.raise_for_status() 
                async with aiofiles.open(temp_file_path, mode='wb') as f:
                    file_size = 0
                    async for chunk in resp.content.iter_chunked(FILE_CHUNK_SIZE):
                        await f.write(chunk)
                        file_size += len(chunk)
                logger.info(f"Успешно скачан '{api_path}' ({file_size} байт) в '{temp_file_path}'")
                return api_path, temp_file_path
        except asyncio.TimeoutError:
            logger.error(f"Тайм-аут при скачивании файла '{api_path}' с {url[:80]}...")
        except aiohttp.ClientResponseError as e:
             logger.error(f"Ошибка HTTP {e.status} при скачивании файла '{api_path}' с {url[:80]}...: {e.message}")
        except Exception as e:
            logger.error(f"Ошибка скачивания файла '{api_path}' с {url[:80]}...: {e}")

        if os.path.exists(temp_file_path):
            try:
                await sync_to_async(os.remove)(temp_file_path)
            except OSError:
                logger.warning(f"Не удалось удалить временный файл '{temp_file_path}' после ошибки скачивания.")
        return None

async def _download_selected_files(
    files_to_download: List[Dict[str, str]],
    temp_dir: str,
    session: aiohttp.ClientSession
) -> Tuple[Dict[str, str], List[str]]:
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY_LIMIT)
    download_tasks = [
        asyncio.create_task(_download_file_to_temp(file_info, temp_dir, session, semaphore))
        for file_info in files_to_download
    ]

    logger.info(f"Запуск {len(download_tasks)} задач скачивания (лимит {DOWNLOAD_CONCURRENCY_LIMIT})...")
    download_results = await asyncio.gather(*download_tasks)

    downloaded_files_map = {}
    failed_downloads = []

    original_api_paths = {info['api_path'] for info in files_to_download}
    processed_api_paths = set()

    for result in download_results:
        if result:
            api_path, temp_path = result
            downloaded_files_map[api_path] = temp_path
            processed_api_paths.add(api_path)

    failed_downloads = list(original_api_paths - processed_api_paths)

    return downloaded_files_map, failed_downloads


def _create_zip_archive_sync(zip_filepath: str, files_to_archive: Dict[str, str]):
    logger.info(f"Начало создания ZIP-архива '{zip_filepath}'...")
    try:
        with zipfile.ZipFile(zip_filepath, 'w', compression=zipfile.ZIP_STORED) as zf:
            for api_path, temp_file_path in files_to_archive.items():
                arcname = api_path
                if arcname.startswith('disk:'):
                    arcname = arcname[5:]
                arcname = arcname.lstrip('/')
                if not arcname:
                    arcname = f"file_{hash(api_path)}"
                logger.debug(f"Добавление в ZIP: '{temp_file_path}' как '{arcname}'")
                zf.write(temp_file_path, arcname=arcname)
        logger.info(f"ZIP-архив '{zip_filepath}' ({os.path.getsize(zip_filepath)} байт) успешно создан.")
        return True
    except Exception as e:
        logger.exception(f"Ошибка при создании ZIP-архива '{zip_filepath}': {e}")
        if os.path.exists(zip_filepath):
            try: os.remove(zip_filepath)
            except OSError: pass
        return False


async def _zip_file_stream_generator(filepath_to_stream: str, zip_filename_for_log: str) -> AsyncGenerator[bytes, None]:
    f = None
    try:
        f = await aiofiles.open(filepath_to_stream, mode='rb')
        while True:
            chunk = await f.read(FILE_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
        logger.info(f"Завершена отправка потока для ZIP '{zip_filename_for_log}'")
    except Exception as e:
        logger.exception(f"Ошибка при стриминге ZIP файла '{filepath_to_stream}': {e}")
        if f and not f.closed:
            try: await f.close()
            except Exception: pass
        raise
    finally:
        # --- Cleanup ---
        if f and not f.closed:
            await f.close()
            logger.debug(f"aiofiles объект закрыт для: {filepath_to_stream}")

        logger.debug(f"Удаление временного ZIP файла: {filepath_to_stream}")
        try:
            await sync_to_async(os.remove)(filepath_to_stream)
            logger.info(f"Временный ZIP файл '{filepath_to_stream}' успешно удален.")
        except FileNotFoundError:
             logger.warning(f"Временный ZIP файл '{filepath_to_stream}' не найден для удаления (возможно, уже удален).")
        except OSError as e:
            logger.error(f"Не удалось удалить временный ZIP файл '{filepath_to_stream}': {e}")


@require_POST
async def download_zip(request: HttpRequest) -> HttpResponse:
    logger.info("Запуск скачивания ZIP-архива (используя временные файлы + zipfile)")
    public_key = await get_session_value(request, 'public_key')
    resource_type = await get_session_value(request, 'resource_type')
    resource_name = await get_session_value(request, 'resource_name', 'yadisk-archive')
    view_mode = await get_session_value(request, 'view_mode', 'public')
    disk_mode = view_mode == 'disk'

    token = await get_yandex_token(request) if disk_mode else None

    if disk_mode and not token:
        logger.warning("Попытка скачать ZIP со своего диска без OAuth-токена.")
        return redirect('viewer:index')

    if not disk_mode and not public_key:
        logger.warning("Попытка скачать ZIP без public_key в сессии.")
        return redirect('viewer:index')

    if resource_type != 'dir':
        logger.warning(f"Попытка скачать ZIP для ресурса не-папки: mode={'disk' if disk_mode else 'public'}, type={resource_type}")
        return redirect('viewer:index')

    selected_paths = request.POST.getlist('selected_files')
    if not selected_paths:
        logger.warning("Попытка скачать ZIP без выбранных файлов.")
        return redirect('viewer:index')

    safe_name = slugify(resource_name) or 'archive'
    zip_filename = f"{safe_name}-{now().strftime('%Y%m%d_%H%M%S')}.zip"
    logger.info(f"Генерация ZIP '{zip_filename}' для {len(selected_paths)} выбранных элементов.")

    temp_dir_obj = tempfile.TemporaryDirectory()
    temp_dir_path = temp_dir_obj.name
    logger.debug(f"Создана временная директория для скачивания: {temp_dir_path}")

    downloaded_files_map: Dict[str, str] = {}
    failed_files: List[str] = []
    zip_temp_filepath: str | None = None

    try:
        async with aiohttp.ClientSession() as session:
            files_ready_for_download, failed_links = await _fetch_download_links(
                public_key, selected_paths, session, token=token if disk_mode else None
            )
            failed_files.extend(failed_links)

            if not files_ready_for_download:
                logger.error("Не удалось получить ни одной ссылки для скачивания.")
                temp_dir_obj.cleanup()

                return HttpResponse("Не удалось получить ссылки для скачивания выбранных файлов.", status=500)

            downloaded_files_map, failed_downloads = await _download_selected_files(
                files_ready_for_download, temp_dir_path, session
            )
            failed_files.extend(failed_downloads)

        if not downloaded_files_map:
            logger.error("Не удалось скачать ни одного файла.")
            temp_dir_obj.cleanup()
            return HttpResponse("Не удалось скачать ни одного из выбранных файлов.", status=500)

        logger.info(f"Скачано {len(downloaded_files_map)} файлов во временную директорию '{temp_dir_path}'.")
        if failed_files:
             logger.warning(f"Не удалось получить ссылку или скачать {len(failed_files)} файлов: {failed_files}")

        zip_temp_file = await sync_to_async(tempfile.NamedTemporaryFile)(delete=False, suffix='.zip')
        
        zip_temp_filepath = zip_temp_file.name
        await sync_to_async(zip_temp_file.close)()
        logger.debug(f"Временный файл для ZIP (системный temp): {zip_temp_filepath}")



        loop = asyncio.get_running_loop()
        zip_success = await loop.run_in_executor(
            None,
            _create_zip_archive_sync,
            zip_temp_filepath,
            downloaded_files_map
        )

        logger.debug(f"Удаление временной директории со скачанными файлами: {temp_dir_path}")
        try:
            temp_dir_obj.cleanup()
            logger.info(f"Временная директория '{temp_dir_path}' успешно очищена.")
        except Exception as e:
             logger.error(f"Ошибка при очистке временной директории '{temp_dir_path}': {e}")


        if not zip_success:
             logger.error(f"Создание ZIP архива '{zip_temp_filepath}' не удалось.")
             return HttpResponseServerError("Ошибка при создании ZIP-архива на сервере.")

        response = FileResponse(
            open(zip_temp_filepath, 'rb'),
            content_type='application/zip'
        )

        try:
            ascii_name = zip_filename.encode('ascii').decode('ascii')
            response['Content-Disposition'] = f'attachment; filename="{ascii_name}"'
        except UnicodeEncodeError:
            encoded_name = quote(zip_filename)
            response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_name}"

        async def _cleanup_zip_file():
            await asyncio.sleep(2)
            try:
                if os.path.exists(zip_temp_filepath):
                    os.remove(zip_temp_filepath)
                    logger.info(f"Временный ZIP файл '{zip_temp_filepath}' удалён.")
            except Exception as e:
                logger.warning(f"Ошибка удаления временного ZIP '{zip_temp_filepath}': {e}")

        asyncio.create_task(_cleanup_zip_file())
        logger.info(f"Отправка ZIP-архива: {zip_filename}")
        return response

    except Exception as e:
        logger.exception(f"Критическая ошибка в обработчике download_zip: {e}")

        # --- Cleanup on critical error ---
        if 'temp_dir_obj' in locals() and hasattr(temp_dir_obj, 'name') and os.path.exists(temp_dir_obj.name):
            try:
                temp_dir_obj.cleanup()
                logger.debug("Временная директория скачивания очищена из-за критической ошибки.")
            except Exception as cleanup_err:
                logger.error(f"Ошибка при очистке временной директории скачивания во время обработки ошибки: {cleanup_err}")

        if zip_temp_filepath and os.path.exists(zip_temp_filepath):
             try:
                 os.remove(zip_temp_filepath)
                 logger.debug("Временный ZIP файл удален из-за критической ошибки.")
             except OSError as remove_err:
                 logger.error(f"Ошибка при удалении временного ZIP файла во время обработки ошибки: {remove_err}")

        return HttpResponseServerError("Внутренняя ошибка сервера при обработке запроса на ZIP-архив.")

@require_POST
def upload_file(request: HttpRequest) -> HttpResponse:
    """Загружает файл на собственный диск пользователя (AJAX или обычный POST)."""
    wants_json = _wants_json(request)
    index_url = reverse('viewer:index')
    view_mode = request.session.get('view_mode', 'public')
    if view_mode != 'disk':
        if wants_json:
            return _json_error('Загрузка доступна только в режиме «Мой диск».', 'public_mode', 403)
        return redirect(index_url)

    token = _get_yandex_token_sync(request)
    if not token:
        if wants_json:
            return _json_error('Сессия авторизации истекла — войдите через Яндекс заново.', 'nologin', 401)
        return redirect(f"{index_url}?source=disk&upload=nologin")

    uploaded = request.FILES.get('file')
    if not uploaded:
        if wants_json:
            return _json_error('Файл не выбран.', 'empty')
        return redirect(f"{index_url}?source=disk&upload=empty")

    current_path = request.POST.get('path') or request.session.get('current_path', '/')
    folder_path = normalize_disk_path(current_path)
    file_name = PurePath(uploaded.name.replace('\\', '/')).name
    name_error = validate_resource_name(file_name)
    if name_error:
        if wants_json:
            return _json_error(name_error, 'invalid_name')
        return redirect(f"{index_url}?source=disk&upload=error")

    target_path = folder_path.rstrip('/') + '/' + file_name
    overwrite = request.POST.get('overwrite', 'true').lower() != 'false'

    back_url = f"{index_url}?source=disk&path={quote(folder_path, safe='')}"
    logger.info(f"Загрузка файла '{target_path}' на свой диск ({uploaded.size} байт, overwrite={overwrite}).")

    href, link_error = get_disk_upload_link_sync(token, target_path, overwrite=overwrite)
    if not href:
        messages_map = {
            'noperm': ('Нет права записи. Добавьте в приложении Яндекса доступ «Яндекс.Диск REST API» с записью и войдите заново.', 403),
            'exists': ('Файл с таким именем уже существует.', 409),
            'network': ('Не удалось связаться с Яндекс.Диском.', 502),
            'error': ('Не удалось подготовить загрузку файла.', 502),
        }
        message, status = messages_map.get(link_error, messages_map['error'])
        logger.warning(f"Загрузка '{target_path}' отклонена: {link_error}")
        if wants_json:
            return _json_error(message, link_error or 'error', status)
        upload_code = 'noperm' if link_error == 'noperm' else 'error'
        return redirect(f"{back_url}&upload={upload_code}")

    try:
        put_resp = requests.put(
            href,
            data=uploaded,
            headers={'Content-Type': 'application/octet-stream'},
            timeout=(30, 7200),
        )
    except requests.RequestException as e:
        logger.exception(f"Сетевая ошибка при загрузке файла '{target_path}': {e}")
        if wants_json:
            return _json_error('Сетевая ошибка при загрузке файла.', 'network', 502)
        return redirect(f"{back_url}&upload=error")

    if put_resp.status_code not in (200, 201, 202, 204):
        logger.error(f"Ошибка загрузки файла '{target_path}': {put_resp.status_code} {put_resp.text[:200]}")
        if wants_json:
            return _json_error(f'Яндекс.Диск вернул ошибку {put_resp.status_code}.', 'error', 502)
        return redirect(f"{back_url}&upload=error")

    logger.info(f"Файл '{target_path}' успешно загружен.")
    if wants_json:
        return JsonResponse({
            'ok': True,
            'name': file_name,
            'path': target_path,
            'size': uploaded.size,
        })
    return redirect(f"{back_url}&upload=ok")


@require_POST
def upload_init(request: HttpRequest) -> HttpResponse:
    """Создаёт сессию чанковой загрузки (обход лимита размера запроса прокси)."""
    token, auth_error = _disk_write_token(request)
    if auth_error:
        return _json_upload_auth_error(auth_error)

    name = (request.POST.get('name') or '').strip()
    name_error = validate_resource_name(name)
    if name_error:
        return _json_error(name_error, 'invalid_name')

    try:
        total_size = int(request.POST.get('size') or 0)
    except (TypeError, ValueError):
        total_size = 0
    if total_size <= 0:
        return _json_error('Не удалось определить размер файла.', 'invalid_size')

    folder_path = normalize_disk_path(request.POST.get('path') or request.session.get('current_path', '/'))
    target_path = folder_path.rstrip('/') + '/' + name
    overwrite = request.POST.get('overwrite', 'true').lower() != 'false'

    _cleanup_stale_uploads()
    upload_id = uuid.uuid4().hex
    directory = _upload_session_dir(upload_id)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        logger.exception("Не удалось создать каталог временной загрузки.")
        return _json_error('Не удалось начать загрузку на сервере.', 'io_error', 500)

    _write_upload_meta(upload_id, {
        'name': name,
        'target_path': target_path,
        'folder_path': folder_path,
        'size': total_size,
        'received_bytes': 0,
        'received_chunks': 0,
        'overwrite': overwrite,
        'created': time.time(),
    })
    logger.info(f"Чанковая загрузка '{target_path}' начата ({total_size} байт), id={upload_id}.")
    return JsonResponse({
        'ok': True,
        'upload_id': upload_id,
        'chunk_size': UPLOAD_CHUNK_DEFAULT_BYTES,
        'min_chunk_size': UPLOAD_CHUNK_MIN_BYTES,
    })


@require_POST
def upload_chunk(request: HttpRequest) -> HttpResponse:
    """Принимает очередную часть файла и дописывает её во временный файл."""
    _token, auth_error = _disk_write_token(request)
    if auth_error:
        return _json_upload_auth_error(auth_error)

    upload_id = (request.POST.get('upload_id') or '').strip()
    meta = _read_upload_meta(upload_id)
    if not meta:
        return _json_error('Сессия загрузки не найдена или истекла.', 'session_expired', 410)

    try:
        offset = int(request.POST.get('offset') or 0)
    except (TypeError, ValueError):
        offset = -1
    received_bytes = int(meta.get('received_bytes', 0))
    if offset != received_bytes:
        return _json_error('Нарушен порядок частей файла.', 'bad_offset', 409)

    chunk = request.FILES.get('chunk')
    if not chunk:
        return _json_error('Часть файла не получена.', 'empty_chunk')
    if chunk.size > UPLOAD_CHUNK_MAX_BYTES:
        return _json_error('Слишком большой размер части файла.', 'chunk_too_large', 413)

    total_size = int(meta.get('size', 0))
    if received_bytes + chunk.size > total_size:
        return _json_error('Размер файла превышает заявленный.', 'size_mismatch')

    directory = _upload_session_dir(upload_id)
    data_path = os.path.join(directory, 'data.part')
    try:
        with open(data_path, 'ab') as out:
            for piece in chunk.chunks():
                out.write(piece)
    except OSError:
        logger.exception(f"Не удалось записать часть файла для upload_id={upload_id}.")
        return _json_error('Не удалось сохранить часть файла.', 'io_error', 500)

    meta['received_bytes'] = received_bytes + chunk.size
    meta['received_chunks'] = int(meta.get('received_chunks', 0)) + 1
    meta['updated'] = time.time()
    _write_upload_meta(upload_id, meta)
    return JsonResponse({
        'ok': True,
        'received_bytes': meta['received_bytes'],
        'received_chunks': meta['received_chunks'],
    })


@require_POST
def upload_complete(request: HttpRequest) -> HttpResponse:
    """Собирает файл и отправляет его на Яндекс.Диск."""
    token, auth_error = _disk_write_token(request)
    if auth_error:
        return _json_upload_auth_error(auth_error)

    upload_id = (request.POST.get('upload_id') or '').strip()
    meta = _read_upload_meta(upload_id)
    if not meta:
        return _json_error('Сессия загрузки не найдена или истекла.', 'session_expired', 410)

    total_size = int(meta.get('size', 0))
    received_bytes = int(meta.get('received_bytes', 0))
    if received_bytes != total_size:
        return _json_error(
            f'Файл загружен не полностью ({received_bytes} из {total_size} байт).',
            'incomplete',
        )

    directory = _upload_session_dir(upload_id)
    data_path = os.path.join(directory, 'data.part')
    if not os.path.exists(data_path):
        return _json_error('Временный файл не найден.', 'session_expired', 410)

    target_path = meta['target_path']
    logger.info(f"Сохранение '{target_path}' на Яндекс.Диск ({received_bytes} байт).")

    href, link_error = get_disk_upload_link_sync(
        token, target_path, overwrite=bool(meta.get('overwrite', True))
    )
    if not href:
        _discard_upload(upload_id)
        message, status = UPLOAD_LINK_ERRORS.get(link_error, UPLOAD_LINK_ERRORS['error'])
        return _json_error(message, link_error or 'error', status)

    try:
        with open(data_path, 'rb') as file_obj:
            put_resp = requests.put(
                href,
                data=file_obj,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=(30, 7200),
            )
    except requests.RequestException as e:
        logger.exception(f"Сетевая ошибка при сохранении '{target_path}': {e}")
        _discard_upload(upload_id)
        return _json_error('Сетевая ошибка при сохранении файла.', 'network', 502)

    if put_resp.status_code not in (200, 201, 202, 204):
        logger.error(f"Ошибка сохранения '{target_path}': {put_resp.status_code} {put_resp.text[:200]}")
        _discard_upload(upload_id)
        return _json_error(f'Яндекс.Диск вернул ошибку {put_resp.status_code}.', 'error', 502)

    _discard_upload(upload_id)
    logger.info(f"Файл '{target_path}' успешно сохранён.")
    return JsonResponse({
        'ok': True,
        'name': meta['name'],
        'path': target_path,
        'size': received_bytes,
    })


@require_POST
def upload_abort(request: HttpRequest) -> HttpResponse:
    """Удаляет временные данные прерванной загрузки."""
    _token, auth_error = _disk_write_token(request)
    if auth_error:
        return _json_upload_auth_error(auth_error)
    _discard_upload((request.POST.get('upload_id') or '').strip())
    return JsonResponse({'ok': True})


@require_POST
def create_folder(request: HttpRequest) -> HttpResponse:
    """Создаёт папку на собственном диске пользователя."""
    wants_json = _wants_json(request)
    index_url = reverse('viewer:index')
    view_mode = request.session.get('view_mode', 'public')
    if view_mode != 'disk':
        if wants_json:
            return _json_error('Создание папок доступно только в режиме «Мой диск».', 'public_mode', 403)
        return redirect(index_url)

    token = _get_yandex_token_sync(request)
    if not token:
        if wants_json:
            return _json_error('Сессия авторизации истекла — войдите через Яндекс заново.', 'nologin', 401)
        return redirect(f"{index_url}?source=disk&upload=nologin")

    name = (request.POST.get('name') or '').strip()
    name_error = validate_resource_name(name)
    if name_error:
        if wants_json:
            return _json_error(name_error, 'invalid_name')
        return redirect(index_url)

    base_path = normalize_disk_path(request.POST.get('path') or request.session.get('current_path', '/'))
    target_path = base_path.rstrip('/') + '/' + name
    logger.info(f"Создание папки '{target_path}' на своём диске.")

    success, error_code = create_disk_folder_sync(token, target_path)
    if not success:
        messages_map = {
            'exists': ('Папка с таким именем уже существует.', 409),
            'noperm': ('Нет права создавать папки на Диске.', 403),
            'network': ('Не удалось связаться с Яндекс.Диском.', 502),
            'error': ('Не удалось создать папку.', 502),
        }
        message, status = messages_map.get(error_code, messages_map['error'])
        if wants_json:
            return _json_error(message, error_code or 'error', status)
        return redirect(f"{index_url}?source=disk&upload=error")

    logger.info(f"Папка '{target_path}' успешно создана.")
    if wants_json:
        return JsonResponse({'ok': True, 'name': name, 'path': target_path})
    return redirect(f"{index_url}?source=disk&path={quote(base_path, safe='')}&upload=folder_ok")


def login_view(request):
    next_url = request.GET.get('next') or settings.LOGIN_REDIRECT_URL
    begin_url = reverse('social:begin', kwargs={'backend': 'yandex-oauth2'})
    return redirect(f"{begin_url}?{urlencode({'next': next_url})}")


def csrf_failure(request: HttpRequest, reason: str = '') -> HttpResponse:
    logger.warning(f"CSRF failure: {reason}")
    return HttpResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Обновите страницу</title></head>'
        '<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:520px;margin:80px auto;padding:0 20px;line-height:1.5">'
        '<h1 style="font-size:22px">Сессия обновилась</h1>'
        '<p>Форма была открыта до входа или обновления сессии, поэтому защитный '
        'токен устарел. Вернитесь на главную и повторите действие.</p>'
        '<p><a href="/">← На главную</a></p>'
        '</body></html>',
        status=403,
        content_type='text/html; charset=utf-8',
    )

def logout_view(request):
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


@require_POST
async def download_all_zip(request: HttpRequest) -> HttpResponse:
    logger.info("Запуск рекурсивного скачивания всех файлов (ZIP)")
    public_key = await get_session_value(request, 'public_key')
    resource_type = await get_session_value(request, 'resource_type')
    resource_name = await get_session_value(request, 'resource_name', 'yadisk-archive')
    view_mode = await get_session_value(request, 'view_mode', 'public')
    current_path = await get_session_value(request, 'current_path', '/')
    disk_mode = view_mode == 'disk'

    token = await get_yandex_token(request) if disk_mode else None

    if disk_mode and not token:
        logger.warning("Попытка рекурсивного скачивания без OAuth-токена.")
        return redirect('viewer:index')

    if not disk_mode and not public_key:
        logger.warning("Попытка скачать ZIP без public_key в сессии.")
        return redirect('viewer:index')

    if resource_type != 'dir':
        logger.warning(f"Попытка скачать ZIP для ресурса не-папки: mode={'disk' if disk_mode else 'public'}, type={resource_type}")
        return redirect('viewer:index')

    safe_name = slugify(resource_name) or 'archive'
    zip_filename = f"{safe_name}-all-{now().strftime('%Y%m%d_%H%M%S')}.zip"
    logger.info(f"Генерация полного ZIP '{zip_filename}' (рекурсивно).")

    temp_dir_obj = tempfile.TemporaryDirectory()
    temp_dir_path = temp_dir_obj.name
    logger.debug(f"Создана временная директория для рекурсивного скачивания: {temp_dir_path}")

    downloaded_files_map: Dict[str, str] = {}
    zip_temp_filepath: str | None = None

    try:
        async with aiohttp.ClientSession() as session:
            if disk_mode:
                all_files = await get_all_disk_files(token, base_path=current_path, session=session)
            else:
                all_files = await get_all_public_files(public_key, base_path='/', session=session)
            if not all_files:
                logger.error("Рекурсивный обход не нашёл ни одного файла.")
                temp_dir_obj.cleanup()
                return HttpResponse("В папке нет файлов для скачивания.", status=400)

            logger.info(f"Найдено {len(all_files)} файлов для рекурсивного скачивания.")
            failed_count = 0
            semaphore = asyncio.Semaphore(RECURSIVE_DOWNLOAD_CONCURRENCY_LIMIT)

            async def _download_one(file_info: dict) -> Tuple[str, str | None]:
                async with semaphore:
                    api_path = file_info['api_path']
                    download_url = file_info['url']
                    temp_path = os.path.join(temp_dir_path, f"{slugify(api_path)}_{id(file_info)}")
                    try:
                        async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=RECURSIVE_DOWNLOAD_TIMEOUT)) as resp:
                            if resp.status == 200:
                                async with aiofiles.open(temp_path, 'wb') as f:
                                    while True:
                                        chunk = await resp.content.read(FILE_CHUNK_SIZE)
                                        if not chunk:
                                            break
                                        await f.write(chunk)
                                return (api_path, temp_path)
                    except Exception as e:
                        logger.error(f"Ошибка при скачивании {api_path}: {e}")
                    return (api_path, None)

            tasks = [_download_one(f) for f in all_files]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Исключение при скачивании: {result}")
                    failed_count += 1
                    continue
                api_path, temp_path = result
                if temp_path and os.path.exists(temp_path):
                    downloaded_files_map[api_path] = temp_path
                else:
                    failed_count += 1

        if not downloaded_files_map:
            logger.error("Не удалось скачать ни одного файла при рекурсивном обходе.")
            temp_dir_obj.cleanup()
            return HttpResponse("Не удалось скачать файлы.", status=500)

        logger.info(f"Рекурсивно скачано {len(downloaded_files_map)} файлов (ошибок: {failed_count}).")

        zip_temp_file = await sync_to_async(tempfile.NamedTemporaryFile)(delete=False, suffix='.zip')
        zip_temp_filepath = zip_temp_file.name
        await sync_to_async(zip_temp_file.close)()

        loop = asyncio.get_running_loop()
        zip_success = await loop.run_in_executor(
            None,
            _create_zip_archive_sync,
            zip_temp_filepath,
            downloaded_files_map
        )

        try:
            temp_dir_obj.cleanup()
        except Exception as e:
            logger.error(f"Ошибка при очистке временной директории: {e}")

        if not zip_success:
            return HttpResponseServerError("Ошибка при создании ZIP-архива на сервере.")

        response = FileResponse(
            open(zip_temp_filepath, 'rb'),
            content_type='application/zip'
        )
        try:
            ascii_name = zip_filename.encode('ascii').decode('ascii')
            response['Content-Disposition'] = f'attachment; filename="{ascii_name}"'
        except UnicodeEncodeError:
            encoded_name = quote(zip_filename)
            response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_name}"

        async def _cleanup_all_zip_file():
            await asyncio.sleep(2)
            try:
                if os.path.exists(zip_temp_filepath):
                    os.remove(zip_temp_filepath)
                    logger.info(f"Временный ZIP файл '{zip_temp_filepath}' удалён.")
            except Exception as e:
                logger.warning(f"Ошибка удаления временного ZIP '{zip_temp_filepath}': {e}")

        asyncio.create_task(_cleanup_all_zip_file())
        logger.info(f"Отправка полного ZIP-архива: {zip_filename}")
        return response

    except Exception as e:
        logger.exception(f"Критическая ошибка в download_all_zip: {e}")
        if 'temp_dir_obj' in locals() and hasattr(temp_dir_obj, 'name') and os.path.exists(temp_dir_obj.name):
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
        if zip_temp_filepath and os.path.exists(zip_temp_filepath):
            try:
                os.remove(zip_temp_filepath)
            except OSError:
                pass
        return HttpResponseServerError("Внутренняя ошибка сервера при обработке запроса на ZIP-архив.")