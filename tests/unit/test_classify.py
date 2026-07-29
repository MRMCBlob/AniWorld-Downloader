"""Classification of finished downloads into series vs. movie.

The site models are duck-typed and inconsistent, so the fakes here deliberately
mimic the shapes that actually occur: metadata on the episode for one site, on
the season or series for another, and properties that raise.
"""

import os

from aniworld.integrations.classify import MOVIE, SERIES, classify


class Series:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Season:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Episode:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_series_episode_from_season_metadata():
    series = Series(title="Highschool DxD", release_year="2012-2018", imdb="tt2230051")
    season = Season(season_number=2, are_movies=False, series=series)
    episode = Episode(season=season, series=series, episode_number=5, is_movie=False)

    info = classify(episode)

    assert info.media_type == SERIES
    assert info.title == "Highschool DxD"
    assert info.year == 2012
    assert info.season == 2
    assert info.episode == 5
    assert info.imdb_id == "tt2230051"


def test_movie_detected_from_episode_flag():
    series = Series(title="Spirited Away", release_year=2001)
    episode = Episode(series=series, season=None, is_movie=True, episode_number=1)

    info = classify(episode)

    assert info.media_type == MOVIE
    assert info.year == 2001


def test_movie_detected_from_season_are_movies():
    series = Series(title="Some Anime", release_year="2015")
    season = Season(season_number=None, are_movies=True, series=series)
    episode = Episode(season=season, series=series, episode_number=1)

    assert classify(episode).media_type == MOVIE


def test_title_cleaned_wins_over_title():
    series = Series(title="Show: The Movie (2020)", title_cleaned="Show The Movie")
    episode = Episode(series=series, season=None, is_movie=True)

    assert classify(episode).title == "Show The Movie"


def test_override_beats_detection():
    series = Series(title="Ambiguous")
    episode = Episode(series=series, season=None, is_movie=True)

    info = classify(episode, override="series")

    assert info.media_type == SERIES
    assert info.reason.startswith("explicit override")


def test_series_without_a_season_is_assumed_to_be_season_one():
    series = Series(title="Flat Show")
    episode = Episode(series=series, season=None, is_movie=False, episode_number=3)

    info = classify(episode)

    assert info.media_type == SERIES
    assert info.season == 1
    assert "assumed season 1" in info.reason


def test_default_media_type_is_the_last_resort():
    series = Series(title="No Signal")
    episode = Episode(series=series, season=None)

    assert classify(episode).media_type == SERIES

    os.environ["ANIWORLD_DEFAULT_MEDIA_TYPE"] = "movie"
    try:
        assert classify(episode).media_type == MOVIE
    finally:
        del os.environ["ANIWORLD_DEFAULT_MEDIA_TYPE"]


def test_a_raising_property_does_not_break_classification():
    class Exploding:
        title = "Broken Show"

        @property
        def release_year(self):
            raise RuntimeError("page no longer parses")

        @property
        def imdb(self):
            raise RuntimeError("network down")

    episode = Episode(series=Exploding(), season=None, is_movie=False, episode_number=1)

    info = classify(episode)

    assert info.title == "Broken Show"
    assert info.year is None
    assert info.imdb_id is None


def test_tmdb_id_is_picked_up_from_the_episode():
    episode = Episode(
        series=Series(title="Cineby Movie", release_year=2020),
        season=None,
        is_movie=True,
        tmdb_id=129,
    )

    assert classify(episode).tmdb_id == "129"


def test_imdb_id_is_extracted_from_a_url():
    series = Series(title="Show", imdb="https://www.imdb.com/title/tt1234567/")
    episode = Episode(series=series, season=None, is_movie=False, episode_number=1)

    assert classify(episode).imdb_id == "tt1234567"


def test_hints_only_exposes_what_the_adapters_consume():
    series = Series(title="Show", release_year=2001, imdb="tt0245429")
    episode = Episode(series=series, season=None, is_movie=True)

    assert set(classify(episode).hints()) == {
        "title",
        "year",
        "season",
        "episode",
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
    }
