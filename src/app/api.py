"""JSON API views for dashboards and external integrations."""

import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET

from app import statistics as stats
from app.models import BasicMedia, MediaTypes, Status
from users.models import MediaSortChoices, MediaStatusChoices, User

logger = logging.getLogger(__name__)

_MEDIA_LIST_PAGE_SIZE = 100


def _serialize_datetime(value):
    """Return an ISO-8601 string for a datetime, or None."""
    if value is None:
        return None
    return value.isoformat()


def _serialize_score(value):
    """Return a JSON-friendly score value."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _user_from_token(token, *, api_name):
    """Resolve a user from an integration token or raise Http404."""
    try:
        return User.objects.get(token=token)
    except User.DoesNotExist as error:
        logger.warning("%s API: invalid token", api_name)
        raise Http404 from error


def _serialize_media_entry(media, media_type):
    """Serialize a tracked media instance for the JSON API."""
    item = media.item
    entry = {
        "id": media.id,
        "media_id": item.media_id,
        "source": item.source,
        "media_type": item.media_type,
        "title": item.title,
        "image": item.image,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "status": media.status,
        "score": _serialize_score(media.score),
        "progress": media.progress,
        "max_progress": getattr(media, "max_progress", None),
        "start_date": _serialize_datetime(media.start_date),
        "end_date": _serialize_datetime(media.end_date),
        "notes": media.notes,
        "created_at": _serialize_datetime(media.created_at),
        "repeats": getattr(media, "repeats", 1),
    }
    if media_type == MediaTypes.TV.value:
        entry["last_watched"] = media.last_watched
    return entry


def _parse_media_list_filters(request):
    """Parse optional status/sort/search query params for media list."""
    status_filter = request.GET.get("status") or MediaStatusChoices.ALL
    if status_filter not in MediaStatusChoices.values:
        status_filter = MediaStatusChoices.ALL

    sort_filter = request.GET.get("sort")
    if sort_filter and sort_filter not in MediaSortChoices.values:
        sort_filter = None

    search = request.GET.get("search") or None
    return status_filter, sort_filter, search


@login_not_required
@require_GET
def api_statistics(request, token):
    """Return consumption statistics for the user identified by token.

    Optional query params match the HTML statistics page:
    ``start-date`` and ``end-date`` as ``YYYY-MM-DD``, or ``all`` for both.
    """
    user = _user_from_token(token, api_name="Statistics")

    start_date, end_date = stats.parse_activity_date_range(request)
    user_media, media_count = stats.get_user_media(user, start_date, end_date)
    status_distribution = stats.get_status_distribution(user_media)
    consumption_stats = stats.get_consumption_stats(user_media, media_count)

    consumption = [
        {
            "media_type": entry["media_type"],
            "value": entry["value"],
            "descriptor": str(entry["descriptor"]),
        }
        for entry in consumption_stats
    ]

    return JsonResponse(
        {
            "start_date": _serialize_datetime(start_date),
            "end_date": _serialize_datetime(end_date),
            "media_count": media_count,
            "in_progress": stats.get_status_total(
                status_distribution,
                Status.IN_PROGRESS.value,
            ),
            "consumption": consumption,
        },
    )


@login_not_required
@require_GET
def api_media_list(request, media_type, token):
    """Return tracked media of ``media_type`` for the user identified by token.

    Optional query params:
    ``status`` (default ``All``), ``sort``, ``search``, and ``page`` (1-based).
    """
    if media_type == MediaTypes.EPISODE.value:
        raise Http404

    user = _user_from_token(token, api_name="Media")
    status_filter, sort_filter, search = _parse_media_list_filters(request)

    media_queryset = BasicMedia.objects.get_media_list(
        user=user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search,
    )

    paginator = Paginator(media_queryset, _MEDIA_LIST_PAGE_SIZE)
    page_number = request.GET.get("page", 1)
    media_page = paginator.get_page(page_number)

    BasicMedia.objects.annotate_max_progress(
        media_page.object_list,
        media_type,
    )

    return JsonResponse(
        {
            "media_type": media_type,
            "count": paginator.count,
            "page": media_page.number,
            "num_pages": paginator.num_pages,
            "results": [
                _serialize_media_entry(media, media_type)
                for media in media_page.object_list
            ],
        },
    )
