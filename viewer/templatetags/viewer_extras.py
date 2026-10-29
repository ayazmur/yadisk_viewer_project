from django import template
from django.utils import timezone
from datetime import datetime
from urllib.parse import urlencode
import mimetypes

register = template.Library()

MONTHS_RU = (
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
)


@register.filter
def media_kind(filename):
    """Возвращает image/video/audio/pdf для файлов, которые можно открыть в браузере."""
    mime_type, _ = mimetypes.guess_type(filename or '')
    if not mime_type:
        return ''
    major = mime_type.split('/')[0]
    if major in ('image', 'video', 'audio'):
        return major
    if mime_type == 'application/pdf':
        return 'pdf'
    return ''


@register.filter
def human_date(value):
    """Короткая дата в стиле Яндекс.Диска: «сегодня 14:05», «вчера», «12 мая 2024»."""
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return value
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    local = timezone.localtime(value)
    today = timezone.localtime(timezone.now()).date()
    delta_days = (today - local.date()).days

    if delta_days == 0:
        return f"сегодня {local:%H:%M}"
    if delta_days == 1:
        return 'вчера'
    if local.year == today.year:
        return f"{local.day} {MONTHS_RU[local.month - 1]}"
    return f"{local.day} {MONTHS_RU[local.month - 1]} {local.year}"

@register.simple_tag
def dict_set(dictionary, key, value):
    new_dict = dictionary.copy()
    new_dict[key] = value
    return new_dict

@register.simple_tag
def dict_del(dictionary, key):
    new_dict = dictionary.copy()
    if key in new_dict:
        del new_dict[key]
    return new_dict


@register.simple_tag
def nav_params(query_dict, disk_mode=False, **kwargs):
    """Копия GET-параметров для ссылки: сохраняет режим и меняет/удаляет ключи."""
    params = query_dict.copy()
    if disk_mode:
        params['source'] = 'disk'
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params

@register.filter
def filter_files(items):
    if not items:
        return []
    return [item for item in items if item.get('type') == 'file']

@register.simple_tag
def available_file_types_map():
    return {
        'image': 'Изображения',
        'document': 'Документы',
        'archive': 'Архивы',
        'video': 'Видео',
        'audio': 'Аудио',
        'other': 'Прочее',
    }

@register.filter
def start_index(paginator_page):
    return paginator_page.start_index()

@register.filter
def end_index(paginator_page):
    return paginator_page.end_index()
