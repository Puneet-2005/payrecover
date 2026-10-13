"""Shared accounting precedence; amounts with contradictory history have no known value."""


def classify_payment(amount_count: int, stored_success: bool, simulated_success: bool) -> str:
    if amount_count != 1:
        return "excluded"
    if stored_success:
        return "already_successful"
    return "simulated_recovered" if simulated_success else "unresolved"
