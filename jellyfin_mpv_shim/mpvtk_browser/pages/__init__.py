"""Page registry — the replacement for the mixins' ``ROUTES`` tables.

A kind listed here is served by a :class:`~.base.Page`; anything absent falls
back to the shell's ``ROUTES`` lookup, so the conversion proceeds one route at
a time with the app shippable throughout. That fallback is the whole reason
this can be done incrementally rather than as a 19-route big bang.

``tests/test_page_contract.py`` checks that no kind is claimed by both a page
and a ``ROUTES`` table — a duplicate would resolve by whichever the shell
consulted first, which is exactly the silent-winner hazard
``test_mpvtk_browser_mixins.py`` was written for.
"""

from .base import Page, PageContext
from .books import AudiobookPage, BookPage, BooksPage
from .byname import ByNamePage
from .detail import DetailPage
from .favorites import FavoritesPage
from .genres import GenresPage
from .grid import GridPage, ListPage, PersonPage
from .music import MusicLibraryPage
from .music_detail import AlbumPage, ArtistPage, MusicGenrePage
from .playlist import PlaylistPage
from .queue_edit import PlaylistEditPage, QueuePage
from .home import HomePage
from .livetv import ChannelPage, LiveTvPage, ProgramPage
from .search import SearchPage
from .season import SeasonPage
from .series import SeriesPage

#: kind -> Page subclass.
PAGES = {
    AudiobookPage.kind: AudiobookPage,
    BookPage.kind: BookPage,
    BooksPage.kind: BooksPage,
    ByNamePage.kind: ByNamePage,
    DetailPage.kind: DetailPage,
    FavoritesPage.kind: FavoritesPage,
    GenresPage.kind: GenresPage,
    AlbumPage.kind: AlbumPage,
    ArtistPage.kind: ArtistPage,
    GridPage.kind: GridPage,
    ListPage.kind: ListPage,
    MusicGenrePage.kind: MusicGenrePage,
    MusicLibraryPage.kind: MusicLibraryPage,
    PlaylistEditPage.kind: PlaylistEditPage,
    PlaylistPage.kind: PlaylistPage,
    QueuePage.kind: QueuePage,
    PersonPage.kind: PersonPage,
    HomePage.kind: HomePage,
    LiveTvPage.kind: LiveTvPage,
    ChannelPage.kind: ChannelPage,
    ProgramPage.kind: ProgramPage,
    SearchPage.kind: SearchPage,
    SeasonPage.kind: SeasonPage,
    SeriesPage.kind: SeriesPage,
}

__all__ = ["PAGES", "Page", "PageContext"]
