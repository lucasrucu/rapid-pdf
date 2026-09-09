"""Asking for the password on an encrypted PDF, and nothing else.

WHY THIS IS ITS OWN MODULE. `core/pdf_document.py` is UI-free by rule: it
decides and reports, and something else opens the dialog. The password round
trip is the only place in the open path that has to ask the user a question
mid-call, so the question lives here and the core keeps its hands clean. Every
caller that opens a file by path wants the same three lines, so they are one
call: `open_with_password`.

WHAT IS NOT HERE, DELIBERATELY: anywhere to put a password. Not a "remember
this password" checkbox, not a per-file cache keyed off the path, not a
module-level dict that lives as long as the app. The characters go from the
QLineEdit into `PDFDocument.unlock`, MuPDF derives the file key inside its own
document handle, and the local goes out of scope. A protected document that
comes back through session restore is a NEW open and asks again, which is the
behaviour the user should be able to count on: the app never holds the key to
the client's certificate package between runs.

The retry count belongs to the document (`unlock_attempts_left`), so the loop
here is just "while it still wants one".
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox


def ask_for_password(pdf, parent=None, ask=None) -> bool:
    """Prompt until `pdf` opens, the user cancels, or the tries run out.

    `pdf` must be a PDFDocument whose `needs_password()` is true, which is what
    an `open()` that returned False on an encrypted file leaves behind. True
    means the document is now open and everything downstream can treat it as
    an ordinary one.

    `ask` is the prompt itself, injected so a test can drive the whole loop
    without a modal dialog. It takes the prompt text and answers the password,
    or None for "cancel". The default is a Qt password-echo input box.
    """
    if not pdf.needs_password():
        return pdf.is_open()
    if ask is None:
        ask = _qt_prompt(parent)

    name = os.path.basename(pdf.locked_path() or "") or "this PDF"
    prompt = f"{name} is password protected.\n\nEnter the password to open it:"
    while pdf.needs_password():
        password = ask(prompt)
        if password is None:
            pdf.cancel_unlock()
            return False
        if pdf.unlock(password):
            return True
        # `unlock` writes the reason, including how many tries are left, and it
        # never repeats the password back. Once the tries run out it lets the
        # locked file go, so `needs_password()` goes false and this loop ends.
        prompt = f"{pdf.last_open_error}\n\nEnter the password to open {name}:"
    if parent is not None and pdf.last_open_error:
        QMessageBox.warning(parent, "Password", pdf.last_open_error)
    return False


def _qt_prompt(parent):
    def ask(prompt: str):
        # QLineEdit.Password echo, so the password is never on screen and never
        # in a screenshot of one. The returned text is used once and dropped.
        text, ok = QInputDialog.getText(
            parent, "Password Required", prompt, QLineEdit.EchoMode.Password)
        return text if ok else None
    return ask


def open_with_password(pdf, path: str, parent=None, ask=None) -> bool:
    """Open `path` into `pdf`, prompting for a password if it needs one.

    THE ONE CALL THE UI NEEDS. A plain file opens exactly as it always did; an
    encrypted one gets the prompt loop; every other failure is left alone with
    its reason in `pdf.last_open_error`, so the caller's existing error box is
    still the right thing to show.
    """
    if pdf.open(path):
        return True
    if pdf.needs_password():
        return ask_for_password(pdf, parent=parent, ask=ask)
    return False
