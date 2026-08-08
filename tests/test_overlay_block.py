"""Telling a modal you can close from a wall you cannot.

The distinction carries real weight: get it wrong in one direction and the agent
hands the window over on every page of a site that nags; get it wrong in the other
and it clicks "close" at a real login page forever.
"""

from __future__ import annotations

from chamber.interrupt.overlay_block import Blocker, Nagging


def nag(**kw) -> Blocker:
    """A sign-in nag, as measured on a real page: 99% of the viewport, a Dismiss
    button, and the whole article still readable behind it."""
    base = dict(
        present=True, dismissible=True, content_behind=4200, area_share=0.99,
        label="modal__overlay", closer="Dismiss", has_password=True,
    )
    base.update(kw)
    return Blocker(**base)


class TestNagVersusWall:
    def test_dismissible_over_real_content_is_a_nag(self):
        b = nag()
        assert b.is_nag and not b.is_wall

    def test_password_field_does_not_make_it_a_wall(self):
        """The whole point. A sign-in *nag* contains a password field, and treating
        that as a login wall turns a forty-page run into forty handoffs."""
        assert nag(has_password=True).is_nag

    def test_nothing_behind_it_is_a_wall(self):
        """A login page is not an overlay over content — it is the content."""
        b = nag(content_behind=80)
        assert b.is_wall and not b.is_nag

    def test_no_closer_is_a_wall_even_with_content_behind(self):
        assert nag(dismissible=False, closer="").is_wall

    def test_absent_blocker_is_neither(self):
        b = Blocker()
        assert not b.is_nag and not b.is_wall
        assert "nothing is covering" in b.describe()

    def test_describe_names_the_closer_for_a_nag(self):
        assert "Dismiss" in nag().describe()

    def test_describe_says_no_close_control_for_a_wall(self):
        assert "no usable close control" in nag(dismissible=False).describe()


class TestNagging:
    def test_counts_per_host(self):
        n = Nagging()
        assert n.record("example.com") == 1
        assert n.record("example.com") == 2
        assert n.record("other.test") == 1

    def test_escalates_after_the_limit(self):
        n = Nagging(limit=3)
        for _ in range(2):
            n.record("example.com")
        assert not n.escalated("example.com")
        n.record("example.com")
        assert n.escalated("example.com")

    def test_escalation_advice_points_at_signing_in_once(self):
        n = Nagging(limit=1)
        n.record("example.com")
        advice = n.advice("example.com")
        assert "ask_human" in advice and "sign in once" in advice

    def test_hosts_are_matched_case_insensitively(self):
        n = Nagging(limit=2)
        n.record("Example.com")
        n.record("example.com")
        assert n.escalated("EXAMPLE.COM")

    def test_unseen_host_has_not_escalated(self):
        assert not Nagging().escalated("example.com")
