"""Tests for the token-authenticated media list JSON API."""

from datetime import UTC, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Anime, Item, MediaTypes, Movie, Sources, Status


class ApiMediaListTests(TestCase):
    """Test GET /api/v1/media/<media_type>/<token>/."""

    def setUp(self):
        """Create a user with a known token and tracked media."""
        credentials = {"username": "apimedia", "password": "12345"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.user.token = "test-media-token"  # noqa: S105
        self.user.save(update_fields=["token"])

        movie_item = Item.objects.create(
            media_id="m1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="https://example.com/movie.jpg",
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=movie_item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    score=Decimal("8.5"),
                    progress=1,
                    notes="Great film",
                    end_date=datetime(2024, 6, 15, tzinfo=UTC),
                ),
            ],
        )

        anime_item = Item.objects.create(
            media_id="a1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="https://example.com/anime.jpg",
        )
        Anime.objects.bulk_create(
            [
                Anime(
                    item=anime_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=12,
                    start_date=datetime(2024, 6, 1, tzinfo=UTC),
                ),
            ],
        )

        self.movie_url = reverse(
            "api_media_list",
            args=[MediaTypes.MOVIE.value, self.user.token],
        )
        self.anime_url = reverse(
            "api_media_list",
            args=[MediaTypes.ANIME.value, self.user.token],
        )

    def test_valid_token_returns_media_list(self):
        """A valid token returns tracked media for the requested kind."""
        response = self.client.get(self.movie_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        data = response.json()
        self.assertEqual(data["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["num_pages"], 1)
        self.assertEqual(len(data["results"]), 1)

        movie = data["results"][0]
        self.assertEqual(movie["media_id"], "m1")
        self.assertEqual(movie["title"], "Test Movie")
        self.assertEqual(movie["status"], Status.COMPLETED.value)
        self.assertEqual(movie["score"], 8.5)
        self.assertEqual(movie["progress"], 1)
        self.assertEqual(movie["max_progress"], 1)
        self.assertEqual(movie["notes"], "Great film")
        self.assertEqual(movie["source"], Sources.MANUAL.value)
        self.assertIsNone(movie["season_number"])
        self.assertIsNone(movie["episode_number"])

    def test_invalid_token_returns_404(self):
        """An unknown token returns 404."""
        response = self.client.get(
            reverse(
                "api_media_list",
                args=[MediaTypes.MOVIE.value, "not-a-real-token"],
            ),
        )
        self.assertEqual(response.status_code, 404)

    def test_status_filter(self):
        """Status query param filters results."""
        response = self.client.get(
            self.anime_url,
            {"status": Status.IN_PROGRESS.value},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["title"], "Test Anime")

        response = self.client.get(
            self.anime_url,
            {"status": Status.COMPLETED.value},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_search_filter(self):
        """Search query param matches titles."""
        response = self.client.get(self.movie_url, {"search": "Test Movie"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

        response = self.client.get(self.movie_url, {"search": "missing"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_episode_media_type_returns_404(self):
        """Episode is not a listable media kind for this endpoint."""
        response = self.client.get(
            reverse(
                "api_media_list",
                args=[MediaTypes.EPISODE.value, self.user.token],
            ),
        )
        self.assertEqual(response.status_code, 404)

    def test_does_not_leak_other_users_media(self):
        """Only media belonging to the token owner is returned."""
        other = get_user_model().objects.create_user(
            username="other",
            password="12345",
        )
        other_item = Item.objects.create(
            media_id="m2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Other Movie",
            image="https://example.com/other.jpg",
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=other_item,
                    user=other,
                    status=Status.COMPLETED.value,
                    progress=1,
                ),
            ],
        )

        response = self.client.get(self.movie_url)
        self.assertEqual(response.status_code, 200)
        titles = [entry["title"] for entry in response.json()["results"]]
        self.assertEqual(titles, ["Test Movie"])
