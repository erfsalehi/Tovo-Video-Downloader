"""Custom Tkinter widgets used by the application."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional, Tuple


class RoundedButton(tk.Canvas):
    """A modern, rounded button rendered on a Canvas with hover styling."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Optional[Callable[[], None]] = None,
        radius: int = 20,
        bg_color: str = "#0066CC",
        hover_color: str = "#0055B3",
        text_color: str = "white",
        font: Tuple = ("Segoe UI", 12, "bold"),
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, **kwargs)
        self.text = text
        self.command = command
        self.radius = radius
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.text_color = text_color
        self.font = font
        self.disabled = False
        self.default_bg = bg_color

        self.bind("<Configure>", self._draw)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw(self, event=None) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        r = self.radius
        self.create_polygon(
            r, 0, width - r, 0,
            width, 0, width, r,
            width, height - r, width, height,
            width - r, height, r, height,
            0, height, 0, height - r,
            0, r, 0, 0,
            fill=self.bg_color, smooth=True,
        )
        self.create_text(
            width / 2, height / 2,
            text=self.text, fill=self.text_color, font=self.font,
        )

    def _on_release(self, event) -> None:
        if not self.disabled and self.command:
            self.command()

    def _on_enter(self, event) -> None:
        if not self.disabled:
            self.bg_color = self.hover_color
            self._draw()
            self.config(cursor="hand2")

    def _on_leave(self, event) -> None:
        if not self.disabled:
            self.bg_color = self.default_bg
            self._draw()
            self.config(cursor="arrow")

    def config_state(self, state: str, text: Optional[str] = None, bg: Optional[str] = None) -> None:
        self.disabled = state == "disabled"
        if text:
            self.text = text
        if bg:
            self.bg_color = bg
            self.default_bg = bg
        self._draw()
        if self.disabled:
            self.config(cursor="arrow")
