"""Import anime watch progress from a Crunchyroll account via etp_rt cookie auth."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)

CRUNCHYROLL_BASE_URL = "https://www.crunchyroll.com"
# Public web client credentials used by official CR web apps for etp_rt_cookie grants.
CRUNCHYROLL_BASIC_AUTH = "bm9haWhkZXZtXzZpeWcwYThsMHE6"
CRUNCHYROLL_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
HISTORY_PAGE_SIZE = 100
HISTORY_MAX_PAGES = 300
PROVIDER_NAME = "CRUNCHYROLL"
COOKIE_DIRNAME = "crunchyroll"
COOKIE_SUFFIX = ".etp_rt"


def cookie_cache_path(user):
    """Return the encrypted on-disk path for a user's cached etp_rt cookie."""
    return Path(settings.BASE_DIR) / "db" / COOKIE_DIRNAME / f"{user.pk}{COOKIE_SUFFIX}"


def has_cached_etp_rt(user):
    """Return whether a cached etp_rt cookie file exists for the user."""
    return cookie_cache_path(user).is_file()


def save_etp_rt(user, etp_rt):
    """Encrypt and persist the etp_rt cookie for later imports."""
    cookie = (etp_rt or "").strip()
    if not cookie:
        msg = _("Crunchyroll etp_rt cookie is required.")
        raise MediaImportError(msg)

    path = cookie_cache_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(helpers.encrypt(cookie), encoding="utf-8")
    logger.info("Cached Crunchyroll etp_rt cookie for user %s", user.username)
    return cookie


def load_etp_rt(user):
    """Load and decrypt the user's cached etp_rt cookie, or None."""
    path = cookie_cache_path(user)
    if not path.is_file():
        return None

    try:
        return helpers.decrypt(path.read_text(encoding="utf-8").strip())
    except (InvalidToken, OSError, ValueError) as error:
        logger.warning(
            "Failed to read cached Crunchyroll cookie for user %s: %s",
            user.username,
            error,
        )
        return None


def clear_etp_rt(user):
    """Delete the user's cached etp_rt cookie file if present."""
    path = cookie_cache_path(user)
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning(
            "Failed to clear cached Crunchyroll cookie for user %s: %s",
            user.username,
            error,
        )
        return False
    logger.info("Cleared cached Crunchyroll etp_rt cookie for user %s", user.username)
    return True


def resolve_etp_rt(user, submitted_cookie):
    """Use a newly submitted cookie (and cache it) or fall back to the file cache."""
    submitted = (submitted_cookie or "").strip()
    if submitted:
        return save_etp_rt(user, submitted)

    cached = load_etp_rt(user)
    if cached:
        return cached

    msg = _(
        "Crunchyroll etp_rt cookie is required. "
        "Paste it once to cache it for later imports."
    )
    raise MediaImportError(msg)


def importer(etp_rt, user, mode):
    """Import the user's Crunchyroll watch history as anime progress."""
    return CrunchyrollImporter(etp_rt, user, mode).import_data()


class CrunchyrollImporter:
    """Fetch Crunchyroll history and match series to MAL/AniList anime."""

    def __init__(self, etp_rt, user, mode):
        """Initialize the importer with cookie, user, and mode.

        Args:
            etp_rt (str): Crunchyroll ``etp_rt`` cookie value
            user: Django user to import data for
            mode (str): Import mode (``new`` or ``overwrite``)
        """
        self.etp_rt = (etp_rt or "").strip()
        self.user = user
        self.mode = mode
        self.warnings = []

        if not self.etp_rt:
            msg = _("Crunchyroll etp_rt cookie is required.")
            raise MediaImportError(msg)

        self.access_token = None
        self.account_id = None
        self.device_id = str(uuid.uuid4())

        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.to_update = defaultdict(dict)

        logger.info(
            "Initialized Crunchyroll importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Authenticate, fetch history, match series, and persist anime."""
        self._authenticate()
        history = self._fetch_watch_history()
        series_progress = self._aggregate_series(history)

        if not series_progress:
            logger.info(
                "No series found in Crunchyroll history for user %s",
                self.user.username,
            )
            return {}, ""

        for series in series_progress.values():
            self._process_series(series)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        created = len(self.bulk_media[MediaTypes.ANIME.value])
        updated = len(self.to_update[MediaTypes.ANIME.value])
        imported_counts = {}
        if created or updated:
            imported_counts[MediaTypes.ANIME.value] = created + updated

        logger.info(
            "Crunchyroll import completed for user %s: %s",
            self.user.username,
            imported_counts,
        )
        return imported_counts, "\n".join(dict.fromkeys(self.warnings))

    def _authenticate(self):
        """Exchange the etp_rt cookie for an access token and account id."""
        url = f"{CRUNCHYROLL_BASE_URL}/auth/v1/token"
        headers = {
            "Authorization": f"Basic {CRUNCHYROLL_BASIC_AUTH}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": CRUNCHYROLL_USER_AGENT,
            "Cookie": f"etp_rt={self.etp_rt}; device_id={self.device_id}",
        }
        data = {
            "grant_type": "etp_rt_cookie",
            "scope": "offline_access",
            "device_id": self.device_id,
            "device_type": "Firefox on Linux",
        }

        try:
            response = services.api_request(
                PROVIDER_NAME,
                "POST",
                url,
                data=data,
                headers=headers,
            )
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status in {
                requests.codes.bad_request,
                requests.codes.unauthorized,
                requests.codes.forbidden,
            }:
                msg = _(
                    "Crunchyroll rejected the etp_rt cookie. "
                    "Copy a fresh etp_rt value from crunchyroll.com while logged in."
                )
                raise MediaImportError(msg) from error
            msg = _("Crunchyroll authentication failed (HTTP %(status)s).") % {
                "status": status,
            }
            raise MediaImportError(msg) from error

        self.access_token = response.get("access_token")
        self.account_id = response.get("account_id")
        if not self.access_token or not self.account_id:
            msg = _("Crunchyroll authentication response was incomplete.")
            raise MediaImportError(msg)

        logger.info("Authenticated with Crunchyroll account %s", self.account_id)

    def _authorized_headers(self):
        """Return headers for authenticated Crunchyroll content requests."""
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": CRUNCHYROLL_USER_AGENT,
        }

    def _fetch_watch_history(self):
        """Paginate the account watch-history endpoint."""
        entries = []
        total = None

        for page in range(1, HISTORY_MAX_PAGES + 1):
            url = f"{CRUNCHYROLL_BASE_URL}/content/v2/{self.account_id}/watch-history"
            try:
                response = services.api_request(
                    PROVIDER_NAME,
                    "GET",
                    url,
                    params={
                        "page": page,
                        "page_size": HISTORY_PAGE_SIZE,
                        "locale": "en-US",
                    },
                    headers=self._authorized_headers(),
                )
            except requests.HTTPError as error:
                status = (
                    error.response.status_code if error.response is not None else None
                )
                msg = _(
                    "Failed to fetch Crunchyroll watch history (HTTP %(status)s)."
                ) % {"status": status}
                raise MediaImportError(msg) from error

            page_entries = response.get("data") or []
            entries.extend(page_entries)
            if total is None:
                total = response.get("total", len(page_entries))

            logger.debug(
                "Fetched Crunchyroll history page %s (%s entries, total=%s)",
                page,
                len(page_entries),
                total,
            )

            if len(page_entries) < HISTORY_PAGE_SIZE:
                break
            if total is not None and len(entries) >= total:
                break
        else:
            msg = _(
                "Crunchyroll watch history exceeded %(max_pages)s pages; aborting."
            ) % {"max_pages": HISTORY_MAX_PAGES}
            raise MediaImportError(msg)

        logger.info(
            "Fetched %s Crunchyroll history entries for account %s",
            len(entries),
            self.account_id,
        )
        return entries

    def _aggregate_series(self, history):
        """Collapse episode history into per-series progress summaries."""
        series = {}

        for entry in history:
            panel = entry.get("panel") or {}
            metadata = panel.get("episode_metadata") or {}
            series_id = metadata.get("series_id")
            series_title = metadata.get("series_title") or panel.get("title")

            if not series_id or not series_title:
                # Movies / missing metadata are out of v1 anime matching scope.
                continue

            episode_id = entry.get("id") or panel.get("id")
            episode_number = metadata.get("episode_number")
            fully_watched = bool(entry.get("fully_watched"))
            date_played = self._parse_date(entry.get("date_played"))

            bucket = series.setdefault(
                series_id,
                {
                    "series_id": series_id,
                    "title": series_title,
                    "episode_ids": set(),
                    "fully_watched_ids": set(),
                    "max_episode": 0,
                    "latest_played": None,
                },
            )

            if episode_id:
                bucket["episode_ids"].add(episode_id)
                if fully_watched:
                    bucket["fully_watched_ids"].add(episode_id)

            if (
                isinstance(episode_number, int)
                and episode_number > bucket["max_episode"]
            ):
                bucket["max_episode"] = episode_number

            if date_played and (
                bucket["latest_played"] is None or date_played > bucket["latest_played"]
            ):
                bucket["latest_played"] = date_played

        return series

    def _parse_date(self, value):
        """Parse a Crunchyroll date string into an aware datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return timezone.make_aware(value) if timezone.is_naive(value) else value
        parsed = parse_datetime(str(value).replace("Z", "+00:00"))
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed

    def _process_series(self, series):
        """Match one Crunchyroll series and create or update anime progress."""
        title = series["title"]
        series_id = series["series_id"]

        try:
            match = self._match_anime(title)
        except services.ProviderAPIError as error:
            logger.warning(
                "Provider error matching Crunchyroll series %s (%s): %s",
                title,
                series_id,
                error,
            )
            self.warnings.append(
                _(
                    "%(title)s (%(series_id)s): "
                    "Provider error while searching: %(error)s"
                )
                % {"title": title, "series_id": series_id, "error": error}
            )
            return

        if not match:
            self.warnings.append(
                _("%(title)s (%(series_id)s): Couldn't find a match in %(source)s")
                % {
                    "title": title,
                    "series_id": series_id,
                    "source": Sources.MAL.label,
                }
            )
            return

        media_id = str(match["media_id"])
        source = match["source"]
        progress, status = self._progress_and_status(series, match)

        existing = self.existing_media[MediaTypes.ANIME.value][source].get(media_id)
        if existing and self.mode == "new":
            if helpers.sync_existing_media(
                existing,
                {
                    "progress": progress,
                    "status": status,
                    "notes": _("Imported from Crunchyroll"),
                },
            ):
                self.to_update[MediaTypes.ANIME.value][existing.pk] = existing
            return

        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            MediaTypes.ANIME.value,
            source,
            media_id,
            self.mode,
        ):
            return

        item, _created = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.ANIME.value,
            defaults={
                "title": match["title"],
                "image": match["image"],
            },
        )

        anime = app.models.Anime(
            item=item,
            user=self.user,
            status=status,
            score=None,
            progress=progress,
            notes=_("Imported from Crunchyroll"),
            start_date=None,
            end_date=series["latest_played"]
            if status == Status.COMPLETED.value
            else None,
        )
        self.bulk_media[MediaTypes.ANIME.value].append(anime)

    def _match_anime(self, title):
        """Search MAL then AniList for an anime matching the series title.

        Provider errors (e.g. invalid MAL_API) skip that source so AniList can
        still match without a configured MyAnimeList key.
        """
        for source in (Sources.MAL.value, Sources.ANILIST.value):
            try:
                results = services.search(
                    MediaTypes.ANIME.value,
                    title,
                    1,
                    source,
                    show_adult_titles=self.user.show_adult_titles,
                ).get("results", [])
                if not results:
                    continue

                result = results[0]
                metadata = services.get_media_metadata(
                    MediaTypes.ANIME.value,
                    str(result["media_id"]),
                    source,
                )
            except services.ProviderAPIError as error:
                logger.warning(
                    "Skipping %s search for %s: %s",
                    source,
                    title,
                    error,
                )
                continue

            return {
                "media_id": result["media_id"],
                "source": source,
                "title": metadata.get("title") or result["title"],
                "image": metadata.get("image") or result.get("image"),
                "max_progress": metadata.get("max_progress"),
            }
        return None

    def _progress_and_status(self, series, match):
        """Derive anime episode progress and status from aggregated history."""
        watched_count = len(series["fully_watched_ids"]) or len(series["episode_ids"])
        progress = max(watched_count, series["max_episode"], 0)
        max_progress = match.get("max_progress")

        if max_progress and progress >= max_progress:
            return max_progress, Status.COMPLETED.value
        if progress > 0:
            return progress, Status.IN_PROGRESS.value
        return 0, Status.PLANNING.value
