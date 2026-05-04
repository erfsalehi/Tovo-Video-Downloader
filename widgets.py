"""Custom Tkinter widgets used by the application."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Tuple


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


class DownloadItem(tk.Frame):
    """A minimal row representing one video's status."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        on_cancel: Callable[[], None],
        bg_color: str,
        text_color: str,
        accent_color: str,
        font_family: str,
    ) -> None:
        super().__init__(parent, bg="white", padx=10, pady=5)
        self.title = title
        self.on_cancel = on_cancel
        self.accent_color = accent_color

        self.columnconfigure(0, weight=3)  # Title
        self.columnconfigure(1, weight=2)  # Progress
        self.columnconfigure(2, minsize=40) # %
        self.columnconfigure(3, minsize=80) # Status

        # Title (truncated)
        short_title = (title[:37] + "..") if len(title) > 40 else title
        self.title_label = tk.Label(
            self, text=short_title, bg="white", fg=text_color,
            font=(font_family, 9), anchor="w",
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        # Progress bar
        style = ttk.Style()
        style.configure(
            "Minimal.Horizontal.TProgressbar",
            thickness=4,
            background=accent_color,
            troughcolor="#F2F2F7",
            borderwidth=0,
        )
        
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(
            self, variable=self.progress_var, maximum=100,
            style="Minimal.Horizontal.TProgressbar",
        )
        self.progress.grid(row=0, column=1, sticky="ew", padx=10)

        self.percent_label = tk.Label(
            self, text="0%", bg="white", fg="#86868B",
            font=(font_family, 8, "bold"),
        )
        self.percent_label.grid(row=0, column=2, sticky="e")

        self.status_label = tk.Label(
            self, text="Waiting", bg="white", fg="#C7C7CC",
            font=(font_family, 8), width=10, anchor="e"
        )
        self.status_label.grid(row=0, column=3, sticky="e", padx=(5, 5))

        self.cancel_btn = tk.Button(
            self, text="✕", font=(font_family, 7),
            command=self.on_cancel, bg="white", fg="#FF3B30",
            relief=tk.FLAT, bd=0, cursor="hand2",
            activebackground="white", activeforeground="#D70A01",
        )
        self.cancel_btn.grid(row=0, column=4, sticky="e")

        # Retry button — hidden until the item fails
        self.retry_btn = tk.Button(
            self, text="↻ Retry", font=(font_family, 7, "bold"),
            bg="white", fg="#FF9500",
            relief=tk.FLAT, bd=0, cursor="hand2",
            activebackground="white", activeforeground="#CC7A00",
        )
        self.retry_btn.grid(row=0, column=5, sticky="e", padx=(4, 0))
        self.retry_btn.grid_remove()  # hidden by default

        # Skip button — lets user dismiss a permanently broken item
        self.skip_btn = tk.Button(
            self, text="✓ Skip", font=(font_family, 7, "bold"),
            bg="white", fg="#34C759",
            relief=tk.FLAT, bd=0, cursor="hand2",
            activebackground="white", activeforeground="#248A3D",
        )
        self.skip_btn.grid(row=0, column=6, sticky="e", padx=(4, 0))
        self.skip_btn.grid_remove()  # hidden by default

    def set_on_retry(self, callback: Callable[[], None]) -> None:
        """Attach the retry callback (called after construction by DownloadManager)."""
        self.retry_btn.config(command=callback)

    def set_on_skip(self, callback: Callable[[], None]) -> None:
        """Attach the skip callback (called after construction by DownloadManager)."""
        self.skip_btn.config(command=callback)

    def update_progress(self, percent: float) -> None:
        self.progress_var.set(percent)
        self.percent_label.config(text=f"{int(percent)}%")
        if self.status_label["text"] not in ("Finished", "Failed", "Cancelled", "Skipped"):
            self.status_label.config(text="Active", fg=self.accent_color)

    def set_status(self, status: str, color: Optional[str] = None) -> None:
        self.status_label.config(text=status)
        if color:
            self.status_label.config(fg=color)

        if status == "Failed":
            self.cancel_btn.grid_remove()
            self.retry_btn.grid()
            self.skip_btn.grid()
        elif status in ("Active", "Retrying...", "Waiting"):
            self.retry_btn.grid_remove()
            self.skip_btn.grid_remove()
            self.cancel_btn.grid()
            # Reset progress bar when retrying
            if status == "Retrying...":
                self.progress_var.set(0)
                self.percent_label.config(text="0%")
        elif status in ("Finished", "Cancelled", "Skipped"):
            self.cancel_btn.grid_remove()
            self.retry_btn.grid_remove()
            self.skip_btn.grid_remove()
            if status == "Finished":
                self.progress_var.set(100)
                self.percent_label.config(text="100%")


class DownloadManager(tk.Frame):
    """An embedded manager for tracking multiple downloads."""

    def __init__(
        self,
        parent: tk.Misc,
        titles: List[str],
        on_cancel_item: Callable[[int], None],
        on_retry_item: Callable[[int], None],
        on_skip_item: Callable[[int], None],
        bg_color: str,
        text_color: str,
        accent_color: str,
        font_family: str,
    ) -> None:
        super().__init__(parent, bg="white", highlightbackground="#D2D2D7", highlightthickness=1)
        
        # Scrollable area
        self.canvas = tk.Canvas(self, bg="white", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg="white")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _on_canvas_configure(event):
            self.canvas.itemconfig(self.canvas_window, width=event.width)
        self.canvas.bind("<Configure>", _on_canvas_configure)

        self.items: List[DownloadItem] = []
        for i, title in enumerate(titles):
            item = DownloadItem(
                self.scrollable_frame, title, lambda idx=i: on_cancel_item(idx),
                bg_color, text_color, accent_color, font_family
            )
            item.set_on_retry(lambda idx=i: on_retry_item(idx))
            item.set_on_skip(lambda idx=i: on_skip_item(idx))
            item.pack(fill=tk.X)
            self.items.append(item)

    def update_item_progress(self, index: int, percent: float) -> None:
        if 0 <= index < len(self.items):
            self.items[index].update_progress(percent)

    def set_item_status(self, index: int, status: str, color: Optional[str] = None) -> None:
        if 0 <= index < len(self.items):
            self.items[index].set_status(status, color)
