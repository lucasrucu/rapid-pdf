"""Rules the theme has to keep. No display needed, none of this touches a widget.

Two things are being locked down: solid colors only (gradients were the thing
Lucas asked to be rid of, and a gradient is one string away from coming back),
and one role per token (the old palette had 21 tokens holding 15 distinct
values, so three of them were copies pretending to be roles).
"""

from ui.theme import DARK, LIGHT, build_qss

PALETTES = (LIGHT, DARK)

# Tokens that legitimately never appear in the stylesheet, with the reason.
NOT_IN_QSS = {
    "canvas": "the QGraphicsView backdrop brush, set in code by apply_palette",
}

# Two tokens may share a value only if that is a deliberate, named decision.
# Empty on purpose: right now every role holds a distinct color.
DELIBERATE_DUPLICATES: set[frozenset[str]] = set()


def test_no_gradients_anywhere():
    for p in PALETTES:
        qss = build_qss(p)
        assert "qlineargradient" not in qss, f"{p.name} grew a gradient back"
        assert "qradialgradient" not in qss
        assert "qconicalgradient" not in qss


def test_every_token_reaches_the_stylesheet():
    """A token nobody references is dead weight, so adding one has to mean
    wiring it up."""
    for p in PALETTES:
        qss = build_qss(p)
        for field in p.color_fields:
            if field in NOT_IN_QSS:
                continue
            assert getattr(p, field) in qss, f"{p.name}.{field} is orphaned"


def test_no_token_is_a_copy_of_another():
    for p in PALETTES:
        seen: dict[str, str] = {}
        for field in p.color_fields:
            value = getattr(p, field).upper()
            other = seen.get(value)
            if other is not None and frozenset((field, other)) in DELIBERATE_DUPLICATES:
                continue
            assert other is None, f"{p.name}: {field} is a copy of {other} ({value})"
            seen[value] = field


def test_light_and_dark_carry_the_same_roles():
    assert LIGHT.color_fields == DARK.color_fields
    assert LIGHT.is_dark is False
    assert DARK.is_dark is True
