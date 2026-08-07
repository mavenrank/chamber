"""URL cleaning — shared by the reader and the trace.

Two callers, one reason: tracking parameters are noise in both places, and they are
expensive in a way that is easy to miss.

Measured on an Amazon India product page, a *single* link in the rendered content:

    https://www.amazon.in/ASUS-.../dp/B0GX4W73SM/ref=dp_prsubs_d_sccl_1/524-5380686
    -6107114?pd_rd_w=QftBT&content-id=amzn1.sym.d73752bb-96cd-462e-bb04-0d81f2ec0fd3
    &pf_rd_p=d73752bb-96cd-462e-bb04-0d81f2ec0fd3&pf_rd_r=06DGKFXB1EK0E2X5MA1A
    &pd_rd_wg=mFoAr&pd_rd_r=2f7e6b30-e0f7-40de-a723-fc7e5a3b7a12&pd_rd_i=B0GX4W73SM

That is 570 characters carrying about 90 characters of meaning. Twenty such links
exhaust a 6,000-character content budget on their own — and on the page where this
was measured, they did: the budget ran out inside the recommendation carousel and
never reached the buy box, so the model could not see that the item was
*Currently unavailable*. It then spent eight steps hunting for an Add to Cart
button that does not exist.

Stripping tracking parameters is always safe: they identify the *referral*, not the
resource. The URL still resolves.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that identify how you arrived, not what you are looking at.
_TRACKING = frozenset(
    {
        # analytics / campaigns
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_cid", "utm_reader", "utm_referrer",
        # ad-network click ids
        "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "dclid", "twclid",
        "ttclid", "igshid", "mc_cid", "mc_eid", "yclid", "_openstat",
        # referral plumbing
        "ref", "ref_", "ref_src", "referrer", "source", "src", "spm", "scm",
        "_ga", "_gl", "cmpid", "campaign_id", "trk", "trkCampaign",
        # Amazon's personalisation/recommendation trail — the worst offender by
        # volume, and pure noise for anyone reading the page.
        "pd_rd_w", "pd_rd_wg", "pd_rd_r", "pd_rd_i", "pf_rd_p", "pf_rd_r",
        "pf_rd_m", "pf_rd_s", "pf_rd_t", "pf_rd_i", "content-id", "psc",
        "qid", "sr", "sprefix", "crid", "th", "linkCode", "linkId", "tag",
        "smid", "dib", "dib_tag",
    }
)


def _strip_path_tracking(path: str) -> str:
    """Drop a trailing `/ref=…` segment and anything after it.

    Not every tracker lives in the query string. Amazon embeds the referral trail
    in the *path* — `/dp/B0GX4W73SM/ref=dp_prsubs_d_sccl_1/524-5380686-6107114` —
    where query-parameter stripping cannot reach it, and that tail alone is over
    half the remaining length.

    The rule is narrow on purpose: a path segment beginning with `ref=` is not a
    resource anywhere, and `/dp/<asin>/ref=…` and `/dp/<asin>` resolve to the same
    page. Everything after it is session identifiers from the same trail.
    """
    segments = path.split("/")
    for i, segment in enumerate(segments):
        if segment.startswith("ref=") or segment.startswith("ref_="):
            return "/".join(segments[:i]) or "/"
    return path


def strip_tracking(url: str) -> str:
    """Remove tracking parameters and the fragment. The URL still resolves."""
    if not url or "://" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING]
    return urlunsplit(
        (parts.scheme, parts.netloc, _strip_path_tracking(parts.path), urlencode(kept), "")
    )


def shorten(url: str, limit: int = 120) -> str:
    """Cleaned, and elided in the middle if it is still enormous.

    The middle is what goes: the host says where, the tail usually carries the
    slug or the product id, and the segments between are the least informative
    part of a long path.
    """
    cleaned = strip_tracking(url)
    if len(cleaned) <= limit:
        return cleaned
    keep = (limit - 3) // 2
    return f"{cleaned[:keep]}…{cleaned[-keep:]}"


def canonical(url: str) -> str:
    """A stable identity for the same page reached by different routes.

    Stronger than `strip_tracking`: also lowercases the host, drops `www.`, and
    normalises the trailing slash. Right for deduplication, wrong for display —
    a canonical URL is for comparing, not for clicking.
    """
    if not url:
        return url
    stripped = strip_tracking(url)
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return stripped

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))
