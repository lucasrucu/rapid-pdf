"""The menu wiring the orchestrator added by hand, which no agent's suite covered.

Each of these is a call main_window makes into document_view or a panel. They
are one AttributeError away from a dead menu item, and the first draft of
selected_page_rows() called a method the Organizer does not have.
"""
import unittest


class MenuWiringReachesRealMethods(unittest.TestCase):
    def test_document_view_exposes_what_main_window_calls(self):
        from ui.document_view import DocumentView
        for name in ("document", "selected_page_rows", "show_status",
                     "has_document"):
            self.assertTrue(callable(getattr(DocumentView, name, None)),
                            f"DocumentView.{name} is what main_window calls")

    def test_both_panels_answer_what_is_selected(self):
        from ui.organizer import PageOrganizer
        from ui.page_panel import PagePanel
        self.assertTrue(callable(getattr(PageOrganizer, "rotate_rows", None)),
                        "the Organizer branch of selected_page_rows calls this")
        self.assertTrue(callable(getattr(PagePanel, "selected_rows", None)),
                        "the strip branch of selected_page_rows calls this")

    def test_both_panels_rotate(self):
        from ui.organizer import PageOrganizer
        from ui.page_panel import PagePanel
        self.assertTrue(callable(getattr(PageOrganizer, "rotate_selected", None)))
        self.assertTrue(callable(getattr(PagePanel, "rotate_selection", None)))

    def test_main_window_exposes_the_actions_its_menu_binds(self):
        from ui.main_window import MainWindow
        for name in ("split_pdf", "_rotate", "combine_pdfs", "print_pdf"):
            self.assertTrue(callable(getattr(MainWindow, name, None)),
                            f"MainWindow.{name} is bound by a menu entry")

    def test_the_password_prompt_entry_point_is_the_one_imported(self):
        import ui.document_view as dv
        self.assertTrue(callable(getattr(dv, "ask_for_password", None)),
                        "open_path calls this on a locked file")
