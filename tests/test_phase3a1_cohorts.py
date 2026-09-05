from hashlib import sha256

import pytest

from payrecover.domain.analytics import (
    AmountBand,
    CohortDimension,
    PaymentCohortV2,
    amount_band_for_paise,
)
from payrecover.domain.models import FieldAvailability


@pytest.mark.parametrize(
    ("amount_paise", "expected"),
    [
        (1, AmountBand.BAND_0),
        (49_999, AmountBand.BAND_0),
        (50_000, AmountBand.BAND_1),
        (50_001, AmountBand.BAND_1),
        (99_999, AmountBand.BAND_1),
        (100_000, AmountBand.BAND_2),
        (100_001, AmountBand.BAND_2),
        (199_999, AmountBand.BAND_2),
        (200_000, AmountBand.BAND_3),
        (200_001, AmountBand.BAND_3),
        (499_999, AmountBand.BAND_3),
        (500_000, AmountBand.BAND_4),
        (500_001, AmountBand.BAND_4),
        (999_999, AmountBand.BAND_4),
        (1_000_000, AmountBand.BAND_5),
        (1_000_001, AmountBand.BAND_5),
    ],
)
def test_amount_band_boundaries(amount_paise: int, expected: AmountBand):
    assert amount_band_for_paise(amount_paise) == expected


@pytest.mark.parametrize("amount_paise", [0, -1, -1_000_000])
def test_amount_band_rejects_nonpositive_values(amount_paise: int):
    with pytest.raises(ValueError, match="must be positive"):
        amount_band_for_paise(amount_paise)


def cohort(
    *,
    issuer_availability: FieldAvailability = FieldAvailability.PROVIDED,
    issuer: str | None = "HDFC",
    provider_availability: FieldAvailability = FieldAvailability.PROVIDED,
    provider: str | None = "Visa",
    method: str = "card",
) -> PaymentCohortV2:
    return PaymentCohortV2(
        merchant_id="acc_TestMerchant",
        method=method,
        issuer=CohortDimension(issuer, issuer_availability),
        provider=CohortDimension(provider, provider_availability),
        amount_band=AmountBand.BAND_3,
    )


def test_canonical_cohort_has_a_fixed_json_and_sha256_vector():
    value = cohort()
    expected_json = (
        '{"amount_band":"band_3","cohort_version":2,'
        '"issuer":{"availability":"provided","value":"HDFC"},'
        '"merchant_id":"acc_TestMerchant","method":"card",'
        '"provider":{"availability":"provided","value":"Visa"}}'
    )
    assert value.canonical_json == expected_json
    assert value.cohort_hash == "6093afc8a55590b69a0e9e50a19fee64fc64ebe45311a556e08c94f29de74a28"
    assert value.cohort_hash == sha256(expected_json.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "availability",
    [
        FieldAvailability.MISSING,
        FieldAvailability.NOT_APPLICABLE,
        FieldAvailability.REDACTED,
    ],
)
def test_unavailable_dimension_states_are_explicit_and_distinct(
    availability: FieldAvailability,
):
    values = {
        state: cohort(issuer_availability=state, issuer=None).cohort_hash
        for state in (
            FieldAvailability.MISSING,
            FieldAvailability.NOT_APPLICABLE,
            FieldAvailability.REDACTED,
        )
    }
    assert len(set(values.values())) == 3
    assert '"value":null' in cohort(
        issuer_availability=availability, issuer=None
    ).canonical_json
    assert "unknown" not in cohort(
        issuer_availability=availability, issuer=None
    ).canonical_json


def test_case_sensitive_stored_dimensions_remain_distinct():
    assert cohort(method="card").cohort_hash != cohort(method="CARD").cohort_hash
    assert cohort(issuer="HDFC").cohort_hash != cohort(issuer="hdfc").cohort_hash


def test_dimension_availability_and_value_must_agree():
    with pytest.raises(ValueError, match="provided dimensions"):
        CohortDimension(None, FieldAvailability.PROVIDED)
    with pytest.raises(ValueError, match="must not contain"):
        CohortDimension("invented", FieldAvailability.MISSING)

