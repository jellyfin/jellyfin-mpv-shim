"""Which gamepad button does what.

The controller is **not a second input model**. Every button here resolves to
something the shim already has: a keyboard key it already binds, or the seek
the arrow keys already perform. That is deliberate and it is the whole design
-- a parallel set of gamepad handlers is a second implementation of the
navigation ladder, and the second implementation is where the drift happens.

Two kinds, and the split is not cosmetic:

* :data:`KEY` is a *synthetic keypress*. The renderer issues the keyboard key
  and whatever is bound to it right now answers -- the browser's spatial
  navigation while the library is up, the playback HUD's summon while a video
  is on, mpv's own default when neither claims it. So the d-pad follows the
  UI from screen to screen without knowing any of them exist, and a page that
  claims ENTER (the epub reader, a dialog) gets the controller's confirm
  button for free.
* :data:`SEEK` goes to ``PlayerManager.kb_seek``, which is the *keyboard's*
  seek and not a number of ours: it reads the distance out of the user's own
  ``input.conf`` binding, applies ``use_web_seek``, and routes through a seek
  that a SyncPlay group hears about. A raw ``seek 5`` from Lua would be none
  of those three, and the third one is a desync the group then corrects.

**The sticks are asymmetric on purpose.** The left stick and d-pad drive the
UI; the right stick seeks. That is why the left stick wakes a hidden playback
HUD rather than falling through to mpv's arrows the way the keyboard does
under the default ``hud_grab_keys``: a keyboard has one set of arrows and has
to share them, and a controller does not.

**mpv names the face buttons by POSITION, not by label.** ``ACTION_DOWN`` is
the bottom button and ``ACTION_RIGHT`` the right one, whatever is printed on
them -- which is exactly the problem, because the two common layouts disagree
about where A is. An Xbox-style pad puts A at the bottom, a Nintendo-style
one (Switch Pro, most 8BitDo) puts it on the right, and SDL reports both by
position. So "confirm is A" is not a statement this code can make, and
``swap_confirm`` is the user telling us which pad is in their hands.

Buttons deliberately left unbound -- ``GAMEPAD_ACTION_UP`` (Y/X),
``GAMEPAD_BACK`` (Select/View) and both triggers -- are free for
``input.conf``. Every binding here is registered *non-forced*, so a line in
``input.conf`` naming the same key wins outright and nothing has to be
disabled first. Inventing a meaning for the spare buttons would take that
away from the people who have a use for them.

Pure data plus one function: no mpv, no settings import, no I/O.
"""

#: Send the keyboard key named in the third field, and let whatever is bound
#: to it answer.
KEY = "key"

#: Seek the way that arrow key seeks. The third field is a ``kb_seek``
#: direction ("up" / "down" / "left" / "right").
SEEK = "seek"

#: Hand the third field to ``PlayerManager.menu_action`` -- the remote
#: control's own ladder. For a button whose meaning *changes* between the
#: library and a playing video and which has no single keyboard key covering
#: both: MENU is the focused item's context menu while browsing and the
#: player's settings menu over a video, and only that ladder knows which.
NAV = "nav"

#: How often a held control may fire, in seconds between events, and **0
#: means it does not auto-repeat at all**.
#:
#: mpv repeats a held key at ``--input-ar-rate``, which defaults to 40 a
#: second. On a keyboard that is a cursor moving through text; on a stick it
#: is forty rows of a library per second, which is not something anybody can
#: aim -- [iw]: "it spams inputs way faster than I can control them". An
#: analog stick is worse than the d-pad again, because an axis resting near
#: the threshold chatters across it and each crossing is a fresh press
#: rather than a repeat -- which is why the limit is applied to every event
#: and not only to the ones mpv marks as repeats.
#:
#: Per control rather than one number, because holding these does not mean
#: the same thing. A direction is "keep going" and wants to feel like a
#: scroll; a page is a jump and wants to be slower; a **seek repeats over
#: real time**, so at the direction's rate a resting thumb would cross a
#: film in a couple of seconds. And confirm, back, play/pause and the menu
#: do not repeat at all: holding a button is not a request to press it
#: again, and an auto-repeating Select activates whatever it landed on over
#: and over.
DIRECTION_REPEAT = 0.15
PAGE_REPEAT = 0.35
SEEK_REPEAT = 0.4
NO_REPEAT = 0

#: The two face buttons that mean confirm and back, in POSITION order --
#: bottom first. ``swap_confirm`` exchanges what they carry, and nothing else
#: on the pad moves.
CONFIRM_BUTTON = "GAMEPAD_ACTION_DOWN"
BACK_BUTTON = "GAMEPAD_ACTION_RIGHT"

#: ``(gamepad key, kind, argument, repeat interval)``, in bind order.
DEFAULT_BINDS = (
    # Direction. The d-pad and the left stick are the same control as far as
    # anything downstream is concerned.
    ("GAMEPAD_DPAD_UP", KEY, "UP", DIRECTION_REPEAT),
    ("GAMEPAD_DPAD_DOWN", KEY, "DOWN", DIRECTION_REPEAT),
    ("GAMEPAD_DPAD_LEFT", KEY, "LEFT", DIRECTION_REPEAT),
    ("GAMEPAD_DPAD_RIGHT", KEY, "RIGHT", DIRECTION_REPEAT),
    ("GAMEPAD_LEFT_STICK_UP", KEY, "UP", DIRECTION_REPEAT),
    ("GAMEPAD_LEFT_STICK_DOWN", KEY, "DOWN", DIRECTION_REPEAT),
    ("GAMEPAD_LEFT_STICK_LEFT", KEY, "LEFT", DIRECTION_REPEAT),
    ("GAMEPAD_LEFT_STICK_RIGHT", KEY, "RIGHT", DIRECTION_REPEAT),

    # Confirm and back. ENTER because that is what the browser's nav
    # activates on and what the hidden HUD wakes on; ESC because it already
    # steps out exactly one layer -- page, dialog, menu, playback -- and a
    # second implementation of that ladder would drift from it. The mouse's
    # back button is routed the same way for the same reason.
    (CONFIRM_BUTTON, KEY, "ENTER", NO_REPEAT),
    (BACK_BUTTON, KEY, "ESC", NO_REPEAT),

    # Play/pause. SPACE rather than `cycle pause`, so it lands on the shim's
    # claim of that key: in a SyncPlay group a local pause is not a pause,
    # it is a desync the group then corrects.
    ("GAMEPAD_ACTION_LEFT", KEY, "SPACE", NO_REPEAT),

    # The context menu of whatever is focused -- Play / Queue / Watched /
    # Favourite / Download -- and the player's settings menu over a video.
    # NAV rather than a MENU keypress because the MENU *key* is a browser
    # nav binding, so over a playing video it is bound to nothing at all and
    # the button would go dead exactly where the remote's hamburger works.
    ("GAMEPAD_START", NAV, "menu", NO_REPEAT),

    # A page at a time through a long library row or list.
    ("GAMEPAD_LEFT_SHOULDER", KEY, "PGUP", PAGE_REPEAT),
    ("GAMEPAD_RIGHT_SHOULDER", KEY, "PGDWN", PAGE_REPEAT),

    # The right stick seeks, with the amounts the arrow keys use.
    ("GAMEPAD_RIGHT_STICK_UP", SEEK, "up", SEEK_REPEAT),
    ("GAMEPAD_RIGHT_STICK_DOWN", SEEK, "down", SEEK_REPEAT),
    ("GAMEPAD_RIGHT_STICK_LEFT", SEEK, "left", SEEK_REPEAT),
    ("GAMEPAD_RIGHT_STICK_RIGHT", SEEK, "right", SEEK_REPEAT),
)


def bindings(swap_confirm=False):
    """The binding table as a list of ``[key, kind, argument, repeat]``.

    Lists rather than tuples because this is JSON on the way to the renderer,
    and a tuple would come back as a list anyway -- pinning the shape here
    keeps the tests honest about what Lua actually receives. The renderer
    reads all four positionally, so the arity is part of the contract.
    """
    swapped = {}
    if swap_confirm:
        swapped = {CONFIRM_BUTTON: BACK_BUTTON, BACK_BUTTON: CONFIRM_BUTTON}
    out = []
    for key, kind, arg, rate in DEFAULT_BINDS:
        out.append([swapped.get(key, key), kind, arg, rate])
    return out
