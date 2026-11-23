import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from requests import Response
from requests.exceptions import HTTPError

from app.models import Anime, MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import crunchyroll, helpers


class ImportCrunchyroll(TestCase):
    """Test importing anime from Crunchyroll watch history."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def _history_page(self, entries, total=None):
        return {"total": total if total is not None else len(entries), "data": entries}

    def _episode_entry(
        self,
        *,
        episode_id,
        series_id,
        series_title,
        episode_number,
        fully_watched=True,
        date_played="2024-01-15T12:00:00Z",
    ):
        return {
            "id": episode_id,
            "fully_watched": fully_watched,
            "date_played": date_played,
            "panel": {
                "id": episode_id,
                "title": f"Episode {episode_number}",
                "episode_metadata": {
                    "episode_number": episode_number,
                    "series_id": series_id,
                    "series_title": series_title,
                },
            },
        }

    @patch("integrations.imports.crunchyroll.services.get_media_metadata")
    @patch("integrations.imports.crunchyroll.services.search")
    @patch("integrations.imports.crunchyroll.services.api_request")
    def test_import_crunchyroll_series(
        self,
        mock_api_request,
        mock_search,
        mock_get_metadata,
    ):
        """Import series aggregated from episode history."""
        mock_api_request.side_effect = [
            {
                "access_token": "token",
                "account_id": "acct-1",
            },
            self._history_page(
                [
                    self._episode_entry(
                        episode_id="ep-1",
                        series_id="GRMG8ZQZR",
                        series_title="One Piece",
                        episode_number=1,
                    ),
                    self._episode_entry(
                        episode_id="ep-2",
                        series_id="GRMG8ZQZR",
                        series_title="One Piece",
                        episode_number=2,
                    ),
                    self._episode_entry(
                        episode_id="ep-movie",
                        series_id="",
                        series_title="",
                        episode_number=1,
                    ),
                ],
            ),
        ]
        mock_search.return_value = {
            "results": [
                {
                    "media_id": 21,
                    "source": Sources.MAL.value,
                    "title": "One Piece",
                    "image": "http://example.com/op.jpg",
                },
            ],
        }
        mock_get_metadata.return_value = {
            "title": "One Piece",
            "image": "http://example.com/op.jpg",
            "max_progress": 1000,
        }

        imported_counts, warnings = crunchyroll.importer(
            "fake-etp-rt",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts[MediaTypes.ANIME.value], 1)
        self.assertEqual(warnings, "")

        anime = Anime.objects.get(user=self.user)
        self.assertEqual(anime.item.title, "One Piece")
        self.assertEqual(anime.item.source, Sources.MAL.value)
        self.assertEqual(anime.progress, 2)
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)

        auth_call, history_call = mock_api_request.call_args_list
        self.assertEqual(auth_call.args[1], "POST")
        self.assertIn("etp_rt=fake-etp-rt", auth_call.kwargs["headers"]["Cookie"])
        self.assertEqual(history_call.args[1], "GET")
        self.assertIn("acct-1/watch-history", history_call.args[2])

    @patch("integrations.imports.crunchyroll.services.api_request")
    def test_import_crunchyroll_invalid_cookie(self, mock_api_request):
        """Reject expired or invalid etp_rt cookies."""
        response = Response()
        response.status_code = 400
        mock_api_request.side_effect = HTTPError(response=response)

        with self.assertRaises(helpers.MediaImportError) as context:
            crunchyroll.importer("bad-cookie", self.user, "new")

        self.assertIn("etp_rt", str(context.exception))

    def test_import_crunchyroll_missing_cookie(self):
        """Require a non-empty etp_rt value."""
        with self.assertRaises(helpers.MediaImportError):
            crunchyroll.importer("   ", self.user, "new")

    @patch("integrations.imports.crunchyroll.services.get_media_metadata")
    @patch("integrations.imports.crunchyroll.services.search")
    @patch("integrations.imports.crunchyroll.services.api_request")
    def test_import_crunchyroll_completed_when_progress_meets_max(
        self,
        mock_api_request,
        mock_search,
        mock_get_metadata,
    ):
        """Mark anime completed when watched progress reaches max episodes."""
        mock_api_request.side_effect = [
            {"access_token": "token", "account_id": "acct-1"},
            self._history_page(
                [
                    self._episode_entry(
                        episode_id="ep-1",
                        series_id="GY5P48XEY",
                        series_title="Cowboy Bebop",
                        episode_number=1,
                    ),
                    self._episode_entry(
                        episode_id="ep-2",
                        series_id="GY5P48XEY",
                        series_title="Cowboy Bebop",
                        episode_number=2,
                    ),
                ],
            ),
        ]
        mock_search.return_value = {
            "results": [
                {
                    "media_id": 1,
                    "source": Sources.MAL.value,
                    "title": "Cowboy Bebop",
                    "image": "http://example.com/cb.jpg",
                },
            ],
        }
        mock_get_metadata.return_value = {
            "title": "Cowboy Bebop",
            "image": "http://example.com/cb.jpg",
            "max_progress": 2,
        }

        crunchyroll.importer("fake-etp-rt", self.user, "new")

        anime = Anime.objects.get(user=self.user)
        self.assertEqual(anime.progress, 2)
        self.assertEqual(anime.status, Status.COMPLETED.value)

    @patch("integrations.imports.crunchyroll.services.search")
    @patch("integrations.imports.crunchyroll.services.api_request")
    def test_import_crunchyroll_unmatched_series_warns(
        self,
        mock_api_request,
        mock_search,
    ):
        """Warn and skip series that cannot be matched."""
        mock_api_request.side_effect = [
            {"access_token": "token", "account_id": "acct-1"},
            self._history_page(
                [
                    self._episode_entry(
                        episode_id="ep-1",
                        series_id="UNKNOWN",
                        series_title="Obscure Show",
                        episode_number=1,
                    ),
                ],
            ),
        ]
        mock_search.return_value = {"results": []}

        imported_counts, warnings = crunchyroll.importer(
            "fake-etp-rt",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts, {})
        self.assertIn("Couldn't find a match", warnings)
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 0)

    @patch("integrations.imports.crunchyroll.services.get_media_metadata")
    @patch("integrations.imports.crunchyroll.services.search")
    @patch("integrations.imports.crunchyroll.services.api_request")
    def test_import_falls_back_to_anilist_when_mal_errors(
        self,
        mock_api_request,
        mock_search,
        mock_get_metadata,
    ):
        """Skip a broken MAL key and match via AniList instead."""
        mock_api_request.side_effect = [
            {"access_token": "token", "account_id": "acct-1"},
            self._history_page(
                [
                    self._episode_entry(
                        episode_id="ep-1",
                        series_id="GG5H5XQ0D",
                        series_title="DAN DA DAN",
                        episode_number=1,
                    ),
                ],
            ),
        ]

        def search_side_effect(_media_type, _query, _page, source, **_kwargs):
            if source == Sources.MAL.value:
                raise services.ProviderAPIError(
                    Sources.MAL.value,
                    Exception("Invalid API key"),
                )
            return {
                "results": [
                    {
                        "media_id": 21202,
                        "source": Sources.ANILIST.value,
                        "title": "DAN DA DAN",
                        "image": "http://example.com/ddd.jpg",
                    },
                ],
            }

        mock_search.side_effect = search_side_effect
        mock_get_metadata.return_value = {
            "title": "DAN DA DAN",
            "image": "http://example.com/ddd.jpg",
            "max_progress": 12,
        }

        imported_counts, warnings = crunchyroll.importer(
            "fake-etp-rt",
            self.user,
            "new",
        )

        self.assertEqual(imported_counts[MediaTypes.ANIME.value], 1)
        self.assertEqual(warnings, "")
        anime = Anime.objects.get(user=self.user)
        self.assertEqual(anime.item.source, Sources.ANILIST.value)


class CrunchyrollCookieCache(TestCase):
    """Test encrypted on-disk caching of the etp_rt cookie."""

    def setUp(self):
        """Create user and isolate cookie files under a temp BASE_DIR."""
        self.credentials = {"username": "cookie-user", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.tmp = Path(tempfile.mkdtemp(prefix="crunchyroll-cookie-"))
        self.settings_override = override_settings(BASE_DIR=self.tmp)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_save_load_and_clear_cookie(self):
        """Persist, read, and delete the encrypted cookie file."""
        crunchyroll.save_etp_rt(self.user, "secret-etp-rt")
        path = crunchyroll.cookie_cache_path(self.user)

        self.assertTrue(path.is_file())
        self.assertNotIn("secret-etp-rt", path.read_text(encoding="utf-8"))
        self.assertTrue(crunchyroll.has_cached_etp_rt(self.user))
        self.assertEqual(crunchyroll.load_etp_rt(self.user), "secret-etp-rt")

        crunchyroll.clear_etp_rt(self.user)
        self.assertFalse(path.exists())
        self.assertIsNone(crunchyroll.load_etp_rt(self.user))

    def test_resolve_uses_cache_when_form_blank(self):
        """Blank submissions reuse the saved cookie."""
        crunchyroll.save_etp_rt(self.user, "cached-cookie")
        self.assertEqual(
            crunchyroll.resolve_etp_rt(self.user, "  "),
            "cached-cookie",
        )

    def test_resolve_replaces_cache_with_new_cookie(self):
        """A new paste overwrites the previous cached cookie."""
        crunchyroll.save_etp_rt(self.user, "old-cookie")
        self.assertEqual(
            crunchyroll.resolve_etp_rt(self.user, "new-cookie"),
            "new-cookie",
        )
        self.assertEqual(crunchyroll.load_etp_rt(self.user), "new-cookie")

    def test_resolve_requires_cookie_when_cache_missing(self):
        """Fail clearly when neither form nor cache has a cookie."""
        with self.assertRaises(helpers.MediaImportError):
            crunchyroll.resolve_etp_rt(self.user, "")
