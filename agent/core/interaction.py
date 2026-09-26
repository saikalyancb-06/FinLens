"""
Shared Agent Interaction System
-------------------------------
Provides the ask() interface for local user interaction prompts (OTP, CAPTCHA image rendering, PDF Password, Security Questions)
taking place directly inside KredoAgent.exe.
Inputs never pass through the server.
"""
import asyncio
import base64
from typing import Optional, Dict, Any

try:
    import tkinter as tk
    from tkinter import simpledialog, messagebox
    from PIL import Image, ImageTk
    import io
    HAS_GUI = True
except Exception:
    HAS_GUI = False


class AgentInteractionSystem:
    """
    Local GUI ask() prompt implementation for KredoAgent.exe.
    Renders visual CAPTCHAs, OTP boxes, and password dialogs directly on the user's desktop.
    """
    def __init__(self, headless_cli_mode: bool = False):
        self.headless_cli_mode = headless_cli_mode

    async def ask(self, interaction_type: str, prompt_data: Dict[str, Any]) -> str:
        """
        Suspend Playwright execution and request local user input.
        Runs GUI popups in a thread to keep async Playwright event loop responsive.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._ask_sync, interaction_type, prompt_data)

    def _ask_sync(self, interaction_type: str, prompt_data: Dict[str, Any]) -> str:
        title = prompt_data.get("title", f"KredoAgent Security Prompt — {interaction_type.upper()}")
        message = prompt_data.get("message", f"Please enter {interaction_type.upper()}:")
        image_bytes = prompt_data.get("image_bytes")

        # 1. Visual CAPTCHA with image rendering
        if interaction_type == "captcha" and image_bytes and HAS_GUI and not self.headless_cli_mode:
            return self._prompt_captcha_gui(title, message, image_bytes)

        # 2. Standard text / OTP / PDF password GUI dialog
        if HAS_GUI and not self.headless_cli_mode:
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            res = simpledialog.askstring(title, message, parent=root)
            root.destroy()
            return res.strip() if res else ""

        # 3. Non-interactive environment (e.g. automated tests or headless CLI without TTY)
        captcha_ans = prompt_data.get("default_value") or "mock123"
        print(f"\n[KredoAgent Security Prompt - Test Mode] {title}: returning mock answer '{captcha_ans}'")
        return captcha_ans

    def _prompt_captcha_gui(self, title: str, message: str, image_bytes: bytes) -> str:
        answer_container = {"value": ""}

        root = tk.Tk()
        root.title(title)
        root.geometry("380x280")
        root.attributes('-topmost', True)
        root.resizable(False, False)

        # Label
        lbl_msg = tk.Label(root, text=message, font=("Arial", 10, "bold"), wraplength=350, pady=10)
        lbl_msg.pack()

        # Render image
        try:
            image = Image.open(io.BytesIO(image_bytes))
            photo = ImageTk.PhotoImage(image)
            lbl_img = tk.Label(root, image=photo, borderwidth=2, relief="groove")
            lbl_img.image = photo  # keep reference
            lbl_img.pack(pady=5)
        except Exception as e:
            tk.Label(root, text=f"[CAPTCHA Image Render Failure: {e}]", fg="red").pack()

        # Input field
        entry = tk.Entry(root, font=("Arial", 12), width=20)
        entry.pack(pady=10)
        entry.focus_set()

        def on_submit(event=None):
            answer_container["value"] = entry.get().strip()
            root.destroy()

        btn_submit = tk.Button(root, text="Submit CAPTCHA", command=on_submit, bg="#0284c7", fg="white", font=("Arial", 10, "bold"), px=10, py=5)
        btn_submit.pack(pady=5)
        root.bind('<Return>', on_submit)

        root.mainloop()
        return answer_container["value"]
