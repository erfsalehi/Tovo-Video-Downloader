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
            fill=self.bg_color, smooth=True, splinesteps=32
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
        self.skip_btn.grid_remove()

        # Transcribe button — appears when finished
        self.transcribe_btn = RoundedButton(
            self, text="✎ Transcribe", 
            radius=12, bg_color=accent_color, hover_color="#4B49B8",
            text_color="white", font=(font_family, 8, "bold"),
            width=90, height=26
        )
        self.transcribe_btn.grid(row=0, column=7, sticky="e", padx=(10, 0))
        self.transcribe_btn.grid_remove()

    def set_on_retry(self, callback: Callable[[], None]) -> None:
        """Attach the retry callback (called after construction by DownloadManager)."""
        self.retry_btn.config(command=callback)

    def set_on_skip(self, callback: Callable[[], None]) -> None:
        """Attach the skip callback (called after construction by DownloadManager)."""
        self.skip_btn.config(command=callback)

    def set_on_transcribe(self, callback: Callable[[], None]) -> None:
        """Attach the transcribe callback."""
        self.transcribe_btn.command = callback

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
                self.transcribe_btn.grid()
            else:
                self.transcribe_btn.grid_remove()
        elif status == "Transcribing...":
            self.transcribe_btn.grid_remove()
            self.status_label.config(text="Transcribing...", fg="#FF9500")


class DownloadManager(tk.Frame):
    """An embedded manager for tracking multiple downloads."""

    def __init__(
        self,
        parent: tk.Misc,
        titles: List[str],
        on_cancel_item: Callable[[int], None],
        on_retry_item: Callable[[int], None],
        on_skip_item: Callable[[int], None],
        on_transcribe_item: Callable[[int], None],
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
            item.set_on_transcribe(lambda idx=i: on_transcribe_item(idx))
            item.pack(fill=tk.X)
            self.items.append(item)

    def update_item_progress(self, index: int, percent: float) -> None:
        if 0 <= index < len(self.items):
            self.items[index].update_progress(percent)

    def set_item_status(self, index: int, status: str, color: Optional[str] = None) -> None:
        if 0 <= index < len(self.items):
            self.items[index].set_status(status, color)
class RoundedEntry(tk.Canvas):
    """A modern, rounded entry field with a focus highlight."""

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        show: Optional[str] = None,
        radius: int = 10,
        bg_color: str = "white",
        border_color: str = "#D2D2D7",
        focus_color: str = "#5E5CE6",
        font: Tuple = ("Segoe UI", 10),
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, height=35, **kwargs)
        self.variable = variable
        self.radius = radius
        self.bg_color = bg_color
        self.border_color = border_color
        self.default_border = border_color
        self.focus_color = focus_color
        
        self.entry = tk.Entry(
            self, textvariable=self.variable, show=show,
            font=font, bg=bg_color, relief=tk.FLAT, bd=0,
            insertbackground="#5E5CE6"
        )
        
        self.bind("<Configure>", self._draw)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

    def _draw(self, event=None) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1: return

        r = self.radius
        self.create_polygon(
            r, 0, w-r, 0, w, 0, w, r,
            w, h-r, w, h, w-r, h, r, h,
            0, h, 0, h-r, 0, r, 0, 0,
            fill=self.bg_color, outline=self.border_color, width=1,
            smooth=True, splinesteps=32
        )
        
        # Position the entry inside the rounded box
        self.create_window(w/2, h/2, window=self.entry, width=w-20, height=h-10)

    def _on_focus_in(self, event) -> None:
        self.border_color = self.focus_color
        self._draw()

    def _on_focus_out(self, event) -> None:
        self.border_color = self.default_border
        self._draw()


class ModernCheckbutton(tk.Canvas):
    """A sleek, modern toggle/checkbox."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        command: Optional[Callable[[], None]] = None,
        bg_color: str = "#F5F5F7",
        active_color: str = "#5E5CE6",
        font: Tuple = ("Segoe UI", 10),
        canvas_width: int = 200,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=bg_color, highlightthickness=0, height=30, width=canvas_width, **kwargs)
        self.text = text
        self.variable = variable
        self.command = command
        self.active_color = active_color
        self.bg_color = bg_color

        # Canvas for the checkbox square only (fixed 22x22)
        self._box = tk.Canvas(
            self, width=22, height=22, bg=bg_color,
            highlightthickness=0, cursor="hand2",
        )
        self._box.pack(side=tk.LEFT, padx=(0, 6))

        # Plain label for text — never clips
        self._label = tk.Label(
            self, text=text, bg=bg_color, fg="#1D1D1F",
            font=font, cursor="hand2",
        )
        self._label.pack(side=tk.LEFT)

        # Bind clicks on both
        for w in (self._box, self._label, self):
            w.bind("<Button-1>", self._toggle)

        self._redraw()

    def _redraw(self) -> None:
        self._box.delete("all")
        size = 22
        r = 6
        color = self.active_color if self.variable.get() else "#D2D2D7"
        # Rounded square
        self._box.create_polygon(
            r, 0,  size-r, 0,  size, 0,  size, r,
            size, size-r,  size, size,  size-r, size,  r, size,
            0, size,  0, size-r,  0, r,  0, 0,
            fill=color, smooth=True, splinesteps=32,
        )
        if self.variable.get():
            # Checkmark
            self._box.create_line(5, 11, 9, 16, 17, 6, fill="white", width=2.5, capstyle="round", joinstyle="round")

    def _toggle(self, event=None) -> None:
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()
        self._redraw()

class RoundedFrame(tk.Canvas):
    """A container with rounded corners and a border."""

    def __init__(
        self,
        parent: tk.Misc,
        radius: int = 20,
        bg_color: str = "white",
        border_color: str = "#D2D2D7",
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, **kwargs)
        self.radius = radius
        self.bg_color = bg_color
        self.border_color = border_color
        
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1: return

        r = self.radius
        self.create_polygon(
            r, 0, w-r, 0, w, 0, w, r,
            w, h-r, w, h, w-r, h, r, h,
            0, h, 0, h-r, 0, r, 0, 0,
            fill=self.bg_color, outline=self.border_color, width=1,
            smooth=True, splinesteps=32
        )
