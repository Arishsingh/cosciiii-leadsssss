"""Lead scoring — decides which builders are worth contacting first.

A list of 400 builders is only useful if the best 50 float to the top. The
weights encode what actually predicts a reply: a reachable named contact beats
a role address, and a developer with several registered projects is a real
business rather than a one-off filing.

Deliberately simple and transparent — the score is explainable to the user in
`score_reasons`, so a bad ranking can be argued with instead of guessed at.
"""

W_EMAIL_VERIFIED = 30
W_EMAIL_UNVERIFIED = 18
W_EMAIL_GENERIC = 12
W_PHONE = 20
W_RERA = 25
W_PROJECTS_MANY = 15   # 5+ registered projects
W_PROJECTS_SOME = 8    # 2-4
W_WEBSITE = 10
W_LINKEDIN = 8      # a second, non-email channel to the firm

MAX_SCORE = 100


def score_lead(rec):
    """Return (score, reasons) for one lead. Score is clamped to 0-100."""
    score = 0
    reasons = []

    conf = (rec.get("email_confidence") or "").strip()
    email = (rec.get("email") or "").strip()
    if email:
        if conf == "verified":
            score += W_EMAIL_VERIFIED
            reasons.append("verified email")
        elif conf == "generic":
            score += W_EMAIL_GENERIC
            reasons.append("role email")
        elif conf == "guess":
            reasons.append("guessed email (not counted)")
        else:
            score += W_EMAIL_UNVERIFIED
            reasons.append("email found on site")

    if (rec.get("phone") or "").strip():
        score += W_PHONE
        reasons.append("phone")

    if (rec.get("rera_number") or "").strip():
        score += W_RERA
        reasons.append("RERA registered")

    projects = int(rec.get("active_projects") or 0)
    if projects >= 5:
        score += W_PROJECTS_MANY
        reasons.append(f"{projects} projects")
    elif projects >= 2:
        score += W_PROJECTS_SOME
        reasons.append(f"{projects} projects")

    if (rec.get("website") or "").strip():
        score += W_WEBSITE
        reasons.append("website")

    if (rec.get("linkedin") or "").strip():
        score += W_LINKEDIN
        reasons.append("LinkedIn")

    return min(score, MAX_SCORE), reasons
