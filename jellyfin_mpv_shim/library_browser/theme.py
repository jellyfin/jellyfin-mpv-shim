"""Dark theme palette and ttk styling for the library browser.

ttk's default themes look dated (Win95-ish buttons/combos). We force the
themable ``clam`` theme and restyle the widget classes we use so everything
matches the dark canvas-based content.
"""

WINDOW_BG = "#15171a"
CARD_BG = "#1e2024"
PANEL_BG = "#26292f"
PLACEHOLDER_BG = "#2a2d33"
BUTTON_BG = "#2e3138"
BUTTON_ACTIVE = "#3a3e46"
ENTRY_BG = "#2a2d33"
BORDER = "#3a3d42"
TEXT_FG = "#e8e8e8"
SUBTLE_FG = "#9aa0a6"
ACCENT = "#00a4dc"


def apply_dark_theme(root, ttk):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=CARD_BG, foreground=TEXT_FG,
                    fieldbackground=ENTRY_BG, bordercolor=BORDER,
                    lightcolor=CARD_BG, darkcolor=CARD_BG,
                    troughcolor=WINDOW_BG, focuscolor=CARD_BG)
    style.configure("TFrame", background=CARD_BG)
    style.configure("TLabel", background=CARD_BG, foreground=TEXT_FG)

    style.configure("TButton", background=BUTTON_BG, foreground=TEXT_FG,
                    bordercolor=BORDER, relief="flat", padding=(10, 5),
                    focuscolor=CARD_BG)
    style.map("TButton",
              background=[("pressed", ACCENT), ("active", BUTTON_ACTIVE),
                          ("disabled", "#212327")],
              foreground=[("disabled", "#5d6168")])

    # Accent (primary action) button variant.
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    relief="flat", padding=(14, 6))
    style.map("Accent.TButton",
              background=[("pressed", "#0086b3"), ("active", "#13b4ea"),
                          ("disabled", "#212327")],
              foreground=[("disabled", "#5d6168")])

    style.configure("TCombobox", fieldbackground=ENTRY_BG, background=BUTTON_BG,
                    foreground=TEXT_FG, arrowcolor=TEXT_FG, bordercolor=BORDER,
                    selectbackground=ENTRY_BG, selectforeground=TEXT_FG,
                    padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", ENTRY_BG)],
              foreground=[("readonly", TEXT_FG)],
              background=[("active", BUTTON_ACTIVE)])

    style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_FG,
                    insertcolor=TEXT_FG, bordercolor=BORDER, padding=4)

    for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(orient, background=BUTTON_BG, troughcolor=WINDOW_BG,
                        bordercolor=WINDOW_BG, arrowcolor=TEXT_FG, relief="flat")
        style.map(orient, background=[("active", BUTTON_ACTIVE)])

    # The Combobox dropdown is a classic Tk Listbox; style it via the option DB.
    root.option_add("*TCombobox*Listbox.background", ENTRY_BG)
    root.option_add("*TCombobox*Listbox.foreground", TEXT_FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    return style
