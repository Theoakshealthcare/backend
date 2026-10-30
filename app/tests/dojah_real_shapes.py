"""Builders for Dojah answers in the REAL shape seen in the live sandbox run of 2026-10-30 (key paths and value types taken from the
redacted captures), with obviously fake values only: fake names, a fake DD-Mon-YYYY date of birth, a fake 11-digit number, reserved
example URLs and addresses. Nothing here comes from the real run. Sandbox facts (kyc_seam D7/D8): selfie.status is true, but
selfie.data.match_score and selfie.data.liveness_score are null and there is no selfie.confidence_value; the pull is wrapped in
``entity`` and the webhook is the same object flat; the BVN entity carries a ``watch_listed`` string (empty in the sandbox)."""
import copy

FAKE_FIRST, FAKE_MIDDLE, FAKE_LAST = "Fakeone", "", "Lasts"
FAKE_DOB = "05-Mar-1971"
FAKE_NUMBER = "10000000099"
FAKE_URL = "https://images.example.invalid/fake.jpg"


def _bvn_entity(number: str, first: str, middle: str, last: str, dob: str) -> dict:
    return {
        "customer": "fake-customer-id", "app_id": None, "bvn": number, "first_name": first, "last_name": last, "middle_name": middle,
        "gender": "Female", "date_of_birth": dob, "phone_number1": "+2340000000001", "phone_number2": "+2340000000002", "image_url": None,
        "email": "fake@example.invalid", "enrollment_bank": "000", "enrollment_branch": "Fake branch", "level_of_account": "Level 1",
        "lga_of_origin": "Fake LGA", "lga_of_residence": "Fake LGA", "marital_status": "Single", "name_on_card": "FAKE NAME",
        "nationality": "Nigeria", "nin": "10000000098", "registration_date": "01-Jan-2000", "residential_address": "1 Fake Street",
        "state_of_origin": "Fake State", "state_of_residence": "Fake State", "title": "Ms", "type": "BASIC", "xc": None, "sc": False,
        "watch_listed": "", "createdAt": "2000-01-01T00:00:00.000Z", "updatedAt": "2000-01-01T00:00:00.000Z",
    }


def real_body(reference_id: str, *, number: str = FAKE_NUMBER, first: str = FAKE_FIRST, middle: str = FAKE_MIDDLE, last: str = FAKE_LAST,
              dob: str = FAKE_DOB, selfie_status: bool = True, government_status: bool = True, verification_status: str = "Completed") -> dict:
    """The verification object as Dojah sends it: this is the WEBHOOK body (flat) and, wrapped in ``{"entity": ...}``, the PULL answer."""
    return copy.deepcopy({
        "metadata": {"ipinfo": {"status": "success", "country": "Fakeland", "city": "Fake City", "district": "", "zip": "000000", "lat": 1.5,
                                "lon": 2.5, "timezone": "Africa/Lagos", "isp": "Fake ISP", "org": "Fake Org", "as": 64500, "mobile": True,
                                "proxy": False, "hosting": False, "query": "192.0.2.1", "region_name": "Fake Region"},
                     "device_info": "Fake device"},
        "data": {
            "index": {"data": {}, "message": "Successfully continued to the main checks.", "status": True},
            "countries": {"data": {"country": "NG"}, "message": "Successfully continued to the next step.", "status": True},
            "government_data": {"data": {"bvn": {"entity": _bvn_entity(number, first, middle, last, dob)}}, "message": "",
                                "status": government_status},
            "selfie": {"data": {"selfie_url": FAKE_URL, "liveness_score": None, "human_reviewed": False, "match_score": None},
                       "message": "Successfully validated your liveness" if selfie_status else "Liveness check failed",
                       "status": selfie_status},
        },
        "id_type": "BVN", "value": number, "message": "Successfully completed the verification.", "verification_pdf": FAKE_URL,
        "reference_id": reference_id, "widget_id": "fake-widget-id", "verification_mode": "LIVENESS", "verification_type": "BVN",
        "verification_value": number, "verification_url": FAKE_URL, "selfie_url": FAKE_URL, "status": True, "aml": {"status": False},
        "verification_status": verification_status,
    })


def real_pull(reference_id: str, **kw) -> dict:
    return {"entity": real_body(reference_id, **kw)}
